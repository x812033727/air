"""變更密碼的測試。

這裡真正要防的不是「密碼沒改成功」,而是**把自己鎖在門外**:改壞了就沒有第二條
路可以進來修。所以每個失敗路徑都要驗證「舊密碼仍然有效」,而不只是「有丟例外」。
"""

from __future__ import annotations

import os
import stat

import bcrypt
import pytest

from app.password import (
    MIN_PASSWORD_LENGTH,
    PasswordError,
    change_password,
    read_credentials,
)


def write_htpasswd(path, username="air", password="initial-password"):
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    path.write_text(f"{username}:{hashed}\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    return path


@pytest.fixture
def htpasswd(tmp_path):
    return write_htpasswd(tmp_path / "htpasswd")


def still_works(path, password):
    return read_credentials(path).verify(password)


class TestReading:
    def test_reads_the_username_and_hash(self, htpasswd):
        credentials = read_credentials(htpasswd)
        assert credentials.username == "air"
        assert credentials.verify("initial-password")

    def test_a_wrong_password_does_not_verify(self, htpasswd):
        assert read_credentials(htpasswd).verify("nope") is False

    def test_a_missing_file_is_an_explicit_error(self, tmp_path):
        with pytest.raises(PasswordError, match="找不到密碼檔"):
            read_credentials(tmp_path / "absent")

    def test_an_empty_file_is_an_explicit_error(self, tmp_path):
        (tmp_path / "htpasswd").write_text("")
        with pytest.raises(PasswordError, match="空的"):
            read_credentials(tmp_path / "htpasswd")

    def test_a_malformed_line_is_an_explicit_error(self, tmp_path):
        (tmp_path / "htpasswd").write_text("no-colon-here\n")
        with pytest.raises(PasswordError, match="格式"):
            read_credentials(tmp_path / "htpasswd")

    def test_a_corrupt_hash_rejects_rather_than_accepting_anything(self, tmp_path):
        """壞掉的雜湊絕不能被讀成「什麼密碼都對」。"""
        (tmp_path / "htpasswd").write_text("air:not-a-bcrypt-hash\n")
        assert read_credentials(tmp_path / "htpasswd").verify("anything") is False


class TestChanging:
    def test_the_new_password_works_afterwards(self, htpasswd):
        change_password(htpasswd, "initial-password", "a-much-better-one")
        assert still_works(htpasswd, "a-much-better-one")

    def test_the_old_password_stops_working(self, htpasswd):
        change_password(htpasswd, "initial-password", "a-much-better-one")
        assert not still_works(htpasswd, "initial-password")

    def test_the_username_is_preserved(self, tmp_path):
        path = write_htpasswd(tmp_path / "htpasswd", username="someone-else")
        assert change_password(path, "initial-password", "a-much-better-one") == "someone-else"
        assert read_credentials(path).username == "someone-else"

    def test_the_file_stays_readable_by_the_web_server(self, htpasswd):
        """nginx 以 www-data 讀這個檔。權限掉了就是所有人都登不進去。"""
        before = htpasswd.stat()
        change_password(htpasswd, "initial-password", "a-much-better-one")
        after = htpasswd.stat()
        assert stat.S_IMODE(after.st_mode) == 0o640
        assert after.st_gid == before.st_gid

    def test_the_group_can_be_pinned_explicitly(self, htpasswd):
        change_password(
            htpasswd, "initial-password", "a-much-better-one", group=os.getgid()
        )
        assert htpasswd.stat().st_gid == os.getgid()

    def test_no_temporary_files_are_left_behind(self, htpasswd):
        change_password(htpasswd, "initial-password", "a-much-better-one")
        siblings = {p.name for p in htpasswd.parent.iterdir()}
        assert siblings == {"htpasswd"}


class TestRefusals:
    """每一種拒絕之後,舊密碼都必須still能用 —— 拒絕不能變成把門鎖死。"""

    def test_a_wrong_current_password_is_refused(self, htpasswd):
        with pytest.raises(PasswordError, match="目前的密碼不正確"):
            change_password(htpasswd, "not-the-password", "a-much-better-one")
        assert still_works(htpasswd, "initial-password")

    def test_a_short_new_password_is_refused(self, htpasswd):
        with pytest.raises(PasswordError, match=str(MIN_PASSWORD_LENGTH)):
            change_password(htpasswd, "initial-password", "short")
        assert still_works(htpasswd, "initial-password")

    def test_reusing_the_same_password_is_refused(self, htpasswd):
        with pytest.raises(PasswordError, match="一樣"):
            change_password(htpasswd, "initial-password", "initial-password")
        assert still_works(htpasswd, "initial-password")

    def test_a_failed_write_leaves_the_old_password_working(self, htpasswd, monkeypatch):
        """磁碟滿了、權限不對 —— 不管什麼原因,結果不能是誰都登不進去。"""
        def explode(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", explode)
        with pytest.raises(PasswordError, match="寫入密碼檔失敗"):
            change_password(htpasswd, "initial-password", "a-much-better-one")

        assert still_works(htpasswd, "initial-password")
        assert {p.name for p in htpasswd.parent.iterdir()} == {"htpasswd"}

    def test_a_file_that_lands_wrong_is_rolled_back(self, htpasswd, monkeypatch):
        """寫進去的位元組才算數,所以寫完要從磁碟讀回來驗一次。"""
        real_write = type(htpasswd).write_text

        def corrupt(self, _contents, *args, **kwargs):
            return real_write(self, "air:definitely-not-a-hash\n")

        monkeypatch.setattr(type(htpasswd), "write_text", corrupt)
        with pytest.raises(PasswordError, match="寫入後驗證失敗"):
            change_password(htpasswd, "initial-password", "a-much-better-one")

        monkeypatch.undo()
        assert still_works(htpasswd, "initial-password")
        # 殘留的 .bak 是第二份密碼雜湊,擺在真檔旁邊沒人負責。
        assert {p.name for p in htpasswd.parent.iterdir()} == {"htpasswd"}

    def test_changing_against_a_missing_file_refuses_cleanly(self, tmp_path):
        with pytest.raises(PasswordError, match="找不到密碼檔"):
            change_password(tmp_path / "absent", "whatever", "a-much-better-one")
