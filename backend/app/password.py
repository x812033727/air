"""變更 HTTP Basic auth 密碼。

站台是用 nginx 的 Basic auth 鎖起來的 —— 認證這件事交給一個被用了三十年的機制,
不是我自己寫的 session。這個模組只做一件很窄的事:**安全地改寫那個密碼檔**。

這裡的失敗模式不是「密碼沒改到」,而是**把你自己鎖在門外**。nginx 是以
`www-data` 身分讀這個檔的,所以只要擁有者、群組或權限在改寫過程中掉了,
或是檔案寫到一半壞掉,結果就是誰都登不進去 —— 包括改密碼的人,而且他手上
沒有第二條路可以進來修。

所以寫入流程是:寫暫存檔 → 設好權限 → **用新密碼實際驗證一次** → 原子置換
→ 再驗一次。任何一步不對就還原,絕不留下一個半殘的密碼檔。
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

import bcrypt

MIN_PASSWORD_LENGTH = 8


class PasswordError(RuntimeError):
    """Something stopped the change; the old password still works."""


@dataclass(frozen=True)
class Credentials:
    username: str
    hash: str

    def verify(self, password: str) -> bool:
        try:
            return bcrypt.checkpw(password.encode(), self.hash.encode())
        except ValueError:
            # A malformed hash must not read as "any password matches".
            return False


def read_credentials(path: Path) -> Credentials:
    if not path.exists():
        raise PasswordError("找不到密碼檔,無法變更密碼。")
    line = path.read_text().strip().splitlines()
    if not line:
        raise PasswordError("密碼檔是空的,無法變更密碼。")
    username, _, hashed = line[0].partition(":")
    if not username or not hashed:
        raise PasswordError("密碼檔格式不正確,無法變更密碼。")
    return Credentials(username=username, hash=hashed)


def change_password(path: Path, current: str, new: str, *, group: int | None = None) -> str:
    """Verify the current password, then replace the file with a new hash.

    Returns the username, so the caller can confirm which account changed.
    Raises :class:`PasswordError` with a message meant for the user; on every
    error path the original file is left exactly as it was.
    """
    credentials = read_credentials(path)

    if not credentials.verify(current):
        raise PasswordError("目前的密碼不正確。")
    if len(new) < MIN_PASSWORD_LENGTH:
        raise PasswordError(f"新密碼至少要 {MIN_PASSWORD_LENGTH} 個字元。")
    if new == current:
        raise PasswordError("新密碼跟目前的密碼一樣。")

    new_hash = bcrypt.hashpw(new.encode(), bcrypt.gensalt()).decode()
    contents = f"{credentials.username}:{new_hash}\n"

    # Verify before anything is written anywhere. A hash that doesn't verify
    # would lock everyone out, and this is the last moment it costs nothing.
    if not bcrypt.checkpw(new.encode(), new_hash.encode()):
        raise PasswordError("新密碼雜湊後驗證失敗,已取消變更。")

    original_stat = path.stat()
    backup = path.with_suffix(".bak")
    temporary = path.with_suffix(".new")

    try:
        shutil.copy2(path, backup)
        temporary.write_text(contents)
        # nginx reads this as www-data. Losing the group or widening the mode
        # is how a password change turns into an outage.
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
        os.chown(temporary, original_stat.st_uid, group if group is not None else original_stat.st_gid)
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        _restore(backup, path)
        raise PasswordError(f"寫入密碼檔失敗:{exc}") from exc

    # Read it back from disk. The bytes nginx will serve are the only ones
    # that count, and this is the only way to know they landed.
    written = read_credentials(path)
    if not written.verify(new):
        _restore(backup, path)
        raise PasswordError("寫入後驗證失敗,已還原原本的密碼。")

    backup.unlink(missing_ok=True)
    return credentials.username


def _restore(backup: Path, path: Path) -> None:
    """Put the old file back and take the copy away.

    A leftover `.bak` is a second file holding a password hash, sitting next to
    the real one with nobody responsible for it.
    """
    if backup.exists():
        shutil.copy2(backup, path)
        backup.unlink(missing_ok=True)
