import os
import shutil


def resolve_ledger_path(mode, app_data_dir, source_dir, migrate_legacy=True):
    filename = "trade_ledger_live.json" if mode == "LIVE" else "trade_ledger_testnet.json"
    path = os.path.join(app_data_dir, filename)
    if not migrate_legacy:
        return path

    legacy_path = os.path.join(source_dir, filename)
    if not os.path.exists(path) and os.path.isfile(legacy_path):
        try:
            os.makedirs(app_data_dir, exist_ok=True)
            shutil.copy2(legacy_path, path)
        except OSError as error:
            raise OSError(
                f"Could not preserve the existing {mode} trade ledger in "
                f"{app_data_dir}: {error}"
            ) from error
    return path
