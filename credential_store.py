import ctypes
import json
import os
import tempfile
from ctypes import wintypes


class DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _crypt(data, protect):
    if os.name != "nt":
        raise OSError("Credential protection is available only on Windows.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source_buffer = ctypes.create_string_buffer(data)
    source = DataBlob(
        len(data),
        ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    destination = DataBlob()
    if protect:
        function = crypt32.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(DataBlob),
        ]
        function.restype = wintypes.BOOL
        success = function(
            ctypes.byref(source),
            "Astra AI Trader API credentials",
            None,
            None,
            None,
            0x1,
            ctypes.byref(destination),
        )
    else:
        function = crypt32.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(DataBlob),
        ]
        function.restype = wintypes.BOOL
        description = wintypes.LPWSTR()
        success = function(
            ctypes.byref(source),
            ctypes.byref(description),
            None,
            None,
            None,
            0x1,
            ctypes.byref(destination),
        )
        if description:
            kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(destination.pbData, ctypes.c_void_p))


class CredentialStore:
    """Encrypts local API credentials using current-Windows-user DPAPI."""

    MODES = ("TESTNET", "LIVE")

    def __init__(self, path):
        self.path = os.path.abspath(path)

    @staticmethod
    def _validate(credentials):
        if not isinstance(credentials, dict):
            raise ValueError("Saved credential data is invalid.")
        if credentials.get("version") != 1:
            raise ValueError("Saved credential data has an unsupported version.")
        if credentials.get("mode") not in CredentialStore.MODES:
            raise ValueError("Saved exchange mode is invalid.")
        modes = credentials.get("credentials")
        if not isinstance(modes, dict):
            raise ValueError("Saved credential set is invalid.")
        normalized = {}
        for mode in CredentialStore.MODES:
            pair = modes.get(mode, {})
            if (
                not isinstance(pair, dict)
                or not isinstance(pair.get("api_key", ""), str)
                or not isinstance(pair.get("api_secret", ""), str)
            ):
                raise ValueError(f"Saved {mode} credentials are invalid.")
            normalized[mode] = {
                "api_key": pair.get("api_key", ""),
                "api_secret": pair.get("api_secret", ""),
            }
        return {
            "version": 1,
            "mode": credentials["mode"],
            "credentials": normalized,
        }

    def save(self, credentials):
        validated = self._validate(credentials)
        plaintext = json.dumps(validated, separators=(",", ":")).encode("utf-8")
        encrypted = _crypt(plaintext, protect=True)
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        descriptor, temp_path = tempfile.mkstemp(
            dir=directory, prefix=".credentials-", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(encrypted)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, self.path)
        except OSError:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise

    def load(self):
        try:
            with open(self.path, "rb") as file:
                encrypted = file.read()
        except FileNotFoundError:
            return None
        plaintext = _crypt(encrypted, protect=False)
        try:
            payload = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Saved credentials could not be decoded.") from error
        return self._validate(payload)

    def clear(self):
        try:
            os.remove(self.path)
        except FileNotFoundError:
            return
