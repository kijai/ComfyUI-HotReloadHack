"""
"""

import os
import sys
import time
import atexit
import hashlib
import logging
import threading
import asyncio
from collections import defaultdict

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import folder_paths
import nodes
from comfy_execution import caching

# ==============================================================================
# === GLOBALS ===
# ==============================================================================

RELOAD_GENERATIONS: dict[str, int] = {}  # Reload count per node class type, mixed into the execution cache keys.
CUSTOM_NODE_ROOT: list[str] = folder_paths.folder_names_and_paths["custom_nodes"][0]  # Custom Node root directory list.

# Set of modules to exclude from reloading.
EXCLUDE_MODULES: set[str] = {'ComfyUI-Manager', 'ComfyUI-HotReloadHack', 'rgthree-comfy'}
if (HOTRELOAD_EXCLUDE := os.getenv("HOTRELOAD_EXCLUDE", None)) is not None:
    EXCLUDE_MODULES.update(x for x in HOTRELOAD_EXCLUDE.split(',') if x)

# Set of modules to observe exclusively for changes.
HOTRELOAD_OBSERVE_ONLY: set[str] = set(x for x in os.getenv("HOTRELOAD_OBSERVE_ONLY", '').split(',') if x)

# File extensions to watch for changes.
HOTRELOAD_EXTENSIONS: set[str] = set(x.strip() for x in os.getenv("HOTRELOAD_EXTENSIONS", '.py,.json,.yaml').split(',') if x)

# Time to wait before reloading after detecting a file change, default is 1.0 second.
try:
    DEBOUNCE_TIME: float = float(os.getenv("HOTRELOAD_DEBOUNCE_TIME", 1.0))
except ValueError:
    DEBOUNCE_TIME = 1.0

# ==============================================================================
# === SUPPORT FUNCTIONS ===
# ==============================================================================

def hash_file(file_path: str) -> str:
    """
    Computes the MD5 hash of a file's contents.

    :param file_path: The path to the file.
    :return: The MD5 hash as a hexadecimal string, or None if an error occurs.
    """
    try:
        with open(file_path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception as e:
        logging.error(f"Error reading file {file_path}: {e}")
        return None

def is_hidden_file_windows(file_path: str) -> bool:
    """
    Check if a given file or directory is hidden on Windows.

    :param file_path: Path to the file or directory.
    :return: True if the file or directory is hidden, False otherwise.
    """
    try:
        import ctypes
        attribute = ctypes.windll.kernel32.GetFileAttributesW(file_path)
        if attribute == -1:
            return False
        return attribute & 0x2 != 0  # FILE_ATTRIBUTE_HIDDEN is 0x2
    except Exception as e:
        logging.error(f"Error checking if file is hidden on Windows: {e}")
        return False

def is_hidden_file(file_path: str) -> bool:
    """
    Check if a given file or any of its parent directories is hidden.

    Works across all major operating systems (Windows, Linux, macOS).

    :param file_path: Path to the file or directory to check.
    :return: True if the file or any parent directory is hidden, False otherwise.
    """
    file_path = os.path.abspath(file_path)

    # Windows-specific hidden file check
    if sys.platform.startswith('win'):
        while file_path and file_path != os.path.dirname(file_path):
            if is_hidden_file_windows(file_path):
                return True
            file_path = os.path.dirname(file_path)
    else:
        # Unix-like systems check
        while file_path and file_path != os.path.dirname(file_path):
            if os.path.basename(file_path).startswith('.'):
                return True
            file_path = os.path.dirname(file_path)

    return False

# ==============================================================================
# === CLASS DEFINITION ===
# ==============================================================================

class DebouncedHotReloader(FileSystemEventHandler):
    """Hot reloader with debouncing mechanism to reload modules on file changes."""

    def __init__(self, delay: float = 1.0):
        """
        Initialize the DebouncedHotReloader.

        :param delay: Delay in seconds before reloading modules after detecting a change.
        """
        self.__delay: float = delay
        self.__last_modified: defaultdict[str, float] = defaultdict(float)
        self.__reload_timers: dict[str, threading.Timer] = {}
        self.__hashes: dict[str, str] = {}
        self.__mtimes: dict[str, float] = {}
        self.__lock: threading.Lock = threading.Lock()
        self.__start_time: float = time.time()

    async def __reload(self, module_name: str) -> bool:
        """
        Reloads a custom node module and bumps the reload generation of the node classes it replaced.

        :param module_name: The name of the module to reload.
        :return: True if the module was reloaded successfully.
        """
        with self.__lock:
            module_path: str = os.path.join(CUSTOM_NODE_ROOT[0], module_name)

            # Unload the module and its submodules, named the way load_custom_node registers them
            if os.path.isfile(module_path):
                sys_module_name: str = os.path.splitext(module_path)[0]
            else:
                sys_module_name = module_path.replace(".", "_x_")
            for mod_name in list(sys.modules.keys()):
                if mod_name == sys_module_name or mod_name.startswith(sys_module_name + "."):
                    del sys.modules[mod_name]

            old_classes: dict = dict(nodes.NODE_CLASS_MAPPINGS)
            if not await nodes.load_custom_node(module_path):
                return False

            for class_type, node_cls in nodes.NODE_CLASS_MAPPINGS.items():
                if old_classes.get(class_type) is not node_cls:
                    RELOAD_GENERATIONS[class_type] = RELOAD_GENERATIONS.get(class_type, 0) + 1
            return True

    def on_modified(self, event):
        """Handles file modification events."""
        if not event.is_directory:
            self.__check_file(event.src_path)

    def on_moved(self, event):
        """Handles files saved by writing a temporary file and renaming it over the original."""
        if not event.is_directory:
            self.__check_file(event.dest_path)

    def __check_file(self, file_path: str):
        """
        Schedules a reload of the module owning the file if its contents changed.

        :param file_path: The path of the changed file.
        """
        # Filter out temporary files created by editors (VS Code, etc.)
        file_name: str = os.path.basename(file_path)
        if file_name.startswith('.') or file_name.endswith(('.tmp', '.swp', '~')) or file_name.startswith('~'):
            return

        if not any(ext == '*' for ext in HOTRELOAD_EXTENSIONS):
            if not any(file_path.endswith(ext) for ext in HOTRELOAD_EXTENSIONS):
                return

        if is_hidden_file(file_path):
            return

        relative_path: str = os.path.relpath(file_path, CUSTOM_NODE_ROOT[0])
        root_dir: str = relative_path.split(os.path.sep)[0]

        if HOTRELOAD_OBSERVE_ONLY and root_dir not in HOTRELOAD_OBSERVE_ONLY:
            return
        elif root_dir in EXCLUDE_MODULES:
            return

        # Quick mtime check to filter out metadata-only changes (common on Windows)
        try:
            current_mtime: float = os.path.getmtime(file_path)
        except OSError:
            # File might not exist anymore
            return

        # First event for a file untouched since startup is not an edit (e.g. NTFS last-access while importing), just record it
        if file_path not in self.__mtimes and current_mtime < self.__start_time:
            self.__mtimes[file_path] = current_mtime
            return

        if current_mtime == self.__mtimes.get(file_path):
            # mtime unchanged, skip hashing
            return

        current_hash: str = hash_file(file_path)
        if current_hash is None:
            # File might not exist anymore (intermediate state during save)
            return

        if current_hash == self.__hashes.get(file_path):
            # Content hasn't changed, just update mtime cache
            self.__mtimes[file_path] = current_mtime
            logging.debug(f"File {file_path} triggered event but content hasn't changed. Ignoring.")
            return

        self.__mtimes[file_path] = current_mtime
        self.__hashes[file_path] = current_hash
        self.schedule_reload(root_dir)

    def schedule_reload(self, module_name: str):
        """
        Schedules a reload of the given module after a delay.

        :param module_name: The name of the module to reload.
        """
        current_time: float = time.time()
        self.__last_modified[module_name] = current_time

        with self.__lock:
            if module_name in self.__reload_timers:
                self.__reload_timers[module_name].cancel()

            timer = threading.Timer(self.__delay, self.check_and_reload, args=[module_name, current_time])
            self.__reload_timers[module_name] = timer
            timer.start()

    def check_and_reload(self, module_name: str, scheduled_time: float):
        """
        Checks the timestamp and reloads the module if needed.

        :param module_name: The name of the module to check.
        :param scheduled_time: The scheduled time for the reload.
        """
        with self.__lock:
            if self.__last_modified[module_name] != scheduled_time:
                return

        try:
            if asyncio.run(self.__reload(module_name)):
                logging.info(f'[ComfyUI-HotReloadHack] Reloaded module {module_name}')
            else:
                logging.warning(f'[ComfyUI-HotReloadHack] Failed to reload module {module_name}')
        except Exception as e:
            logging.exception(f"[ComfyUI-HotReloadHack] {e}")

class HotReloaderService:
    """Service to manage the hot reloading of modules."""

    def __init__(self, delay: float = 1.0):
        """
        Initialize the HotReloaderService.

        :param delay: Delay in seconds before reloading modules after detecting a change.
        """
        self.__observer: Observer = None
        self.__reloader: DebouncedHotReloader = DebouncedHotReloader(delay)

    def start(self):
        """Start observing for file changes."""
        self.__observer = Observer()
        self.__observer.schedule(self.__reloader, CUSTOM_NODE_ROOT[0], recursive=True)
        self.__observer.start()

    def stop(self):
        """Stop observing for file changes."""
        if self.__observer:
            self.__observer.stop()
            self.__observer.join()

# ==============================================================================
# === MONKEY PATCHING ===
# ==============================================================================

def monkeypatch():
    """Mix the reload generation of node classes into the execution cache keys."""

    original_get_immediate_node_signature = caching.CacheKeySetInputSignature.get_immediate_node_signature

    async def get_immediate_node_signature(self, dynprompt, node_id, ancestor_order_mapping):
        """Output cache key. Signatures include all ancestors, so everything downstream of a reloaded node re-executes too."""
        signature = await original_get_immediate_node_signature(self, dynprompt, node_id, ancestor_order_mapping)
        if dynprompt.has_node(node_id):
            generation = RELOAD_GENERATIONS.get(dynprompt.get_node(node_id)["class_type"])
            if generation is not None:
                signature.append(("HOTRELOAD", generation))
        return signature

    original_add_keys = caching.CacheKeySetID.add_keys

    async def add_keys(self, node_ids):
        """Node instance cache key, so a reloaded node gets an instance of the new class."""
        await original_add_keys(self, node_ids)
        for node_id, key in self.keys.items():
            generation = RELOAD_GENERATIONS.get(key[1])
            if generation is not None:
                self.keys[node_id] = key[:2] + (generation,)

    caching.CacheKeySetInputSignature.get_immediate_node_signature = get_immediate_node_signature
    caching.CacheKeySetID.add_keys = add_keys

def setup():
    """Sets up the hot reload system."""
    logging.info("[ComfyUI-HotReloadHack] Patching execution cache keys")
    monkeypatch()
    logging.info("[ComfyUI-HotReloadHack] Starting Hot Reloader")
    hot_reloader_service = HotReloaderService(delay=DEBOUNCE_TIME)
    atexit.register(hot_reloader_service.stop)
    hot_reloader_service.start()

setup()

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
