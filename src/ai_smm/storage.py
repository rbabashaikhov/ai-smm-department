from __future__ import annotations

import mimetypes
import os
from pathlib import Path, PurePosixPath
from typing import Final

import httpx
import paramiko
from dotenv import load_dotenv


load_dotenv()


class StorageUploader:
    """
    Uploads local files to the public file storage over SFTP
    and returns a public HTTPS URL.

    Example:

        uploader = StorageUploader()

        url = uploader.upload(
            local_path="artifacts/post-image.jpg",
            remote_path="ai-smm/ai-catalog-consultant/post-image.jpg",
        )

        print(url)
    """

    DEFAULT_PUBLIC_BASE_URL: Final[str] = "https://files.apps.leadmeter.ru"
    DEFAULT_REMOTE_ROOT: Final[str] = "/srv/miniapps/file-storage"
    DEFAULT_SSH_PORT: Final[int] = 22

    def __init__(
        self,
        *,
        ssh_host: str | None = None,
        ssh_port: int | None = None,
        ssh_user: str | None = None,
        ssh_key_path: str | None = None,
        ssh_password: str | None = None,
        remote_root: str | None = None,
        public_base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.ssh_host = ssh_host or os.getenv("STORAGE_SSH_HOST", "")
        self.ssh_port = ssh_port or int(
            os.getenv("STORAGE_SSH_PORT", str(self.DEFAULT_SSH_PORT))
        )
        self.ssh_user = ssh_user or os.getenv("STORAGE_SSH_USER", "")
        self.ssh_key_path = ssh_key_path or os.getenv("STORAGE_SSH_KEY_PATH")
        self.ssh_password = ssh_password or os.getenv("STORAGE_SSH_PASSWORD")

        self.remote_root = (
            remote_root
            or os.getenv("STORAGE_REMOTE_ROOT")
            or self.DEFAULT_REMOTE_ROOT
        ).rstrip("/")

        self.public_base_url = (
            public_base_url
            or os.getenv("STORAGE_PUBLIC_BASE_URL")
            or self.DEFAULT_PUBLIC_BASE_URL
        ).rstrip("/")

        self.timeout = timeout

        self._validate_config()

    def _validate_config(self) -> None:
        if not self.ssh_host:
            raise ValueError(
                "STORAGE_SSH_HOST is not configured."
            )

        if not self.ssh_user:
            raise ValueError(
                "STORAGE_SSH_USER is not configured."
            )

        if not self.ssh_key_path and not self.ssh_password:
            raise ValueError(
                "Configure STORAGE_SSH_KEY_PATH or STORAGE_SSH_PASSWORD."
            )

        if self.ssh_key_path:
            expanded_key_path = Path(self.ssh_key_path).expanduser()

            if not expanded_key_path.exists():
                raise FileNotFoundError(
                    f"SSH private key not found: {expanded_key_path}"
                )

            self.ssh_key_path = str(expanded_key_path)

    def upload(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        verify_public_url: bool = True,
    ) -> str:
        """
        Upload a local file to storage.

        Parameters
        ----------
        local_path:
            Path to the local file.

        remote_path:
            Relative path inside STORAGE_REMOTE_ROOT.

            Example:
                ai-smm/ai-catalog-consultant/post.jpg

        verify_public_url:
            After upload, make an HTTPS request to make sure
            the file is publicly accessible.

        Returns
        -------
        str
            Public HTTPS URL.
        """

        local_file = Path(local_path).expanduser().resolve()

        if not local_file.exists():
            raise FileNotFoundError(
                f"Local file not found: {local_file}"
            )

        if not local_file.is_file():
            raise ValueError(
                f"Path is not a file: {local_file}"
            )

        normalized_remote_path = self._normalize_remote_path(remote_path)

        remote_file = str(
            PurePosixPath(self.remote_root)
            / PurePosixPath(normalized_remote_path)
        )

        remote_directory = str(
            PurePosixPath(remote_file).parent
        )

        ssh_client = self._create_ssh_client()

        try:
            sftp = ssh_client.open_sftp()

            try:
                self._mkdir_recursive(
                    sftp=sftp,
                    remote_directory=remote_directory,
                )

                sftp.put(
                    str(local_file),
                    remote_file,
                )

                sftp.chmod(
                    remote_file,
                    0o644,
                )

            finally:
                sftp.close()

        finally:
            ssh_client.close()

        public_url = self.build_public_url(
            normalized_remote_path
        )

        if verify_public_url:
            self.verify_url(public_url)

        return public_url

    def upload_bytes(
        self,
        content: bytes,
        remote_path: str,
        *,
        verify_public_url: bool = True,
    ) -> str:
        """
        Upload binary content without requiring a permanent local file.

        Useful later for generated images.
        """

        if not content:
            raise ValueError(
                "Cannot upload empty content."
            )

        normalized_remote_path = self._normalize_remote_path(remote_path)

        remote_file = str(
            PurePosixPath(self.remote_root)
            / PurePosixPath(normalized_remote_path)
        )

        remote_directory = str(
            PurePosixPath(remote_file).parent
        )

        ssh_client = self._create_ssh_client()

        try:
            sftp = ssh_client.open_sftp()

            try:
                self._mkdir_recursive(
                    sftp=sftp,
                    remote_directory=remote_directory,
                )

                with sftp.file(
                    remote_file,
                    mode="wb",
                ) as remote_handle:
                    remote_handle.write(content)

                sftp.chmod(
                    remote_file,
                    0o644,
                )

            finally:
                sftp.close()

        finally:
            ssh_client.close()

        public_url = self.build_public_url(
            normalized_remote_path
        )

        if verify_public_url:
            self.verify_url(public_url)

        return public_url

    def build_public_url(
        self,
        remote_path: str,
    ) -> str:
        normalized_remote_path = self._normalize_remote_path(
            remote_path
        )

        return (
            f"{self.public_base_url}/"
            f"{normalized_remote_path}"
        )

    def verify_url(
        self,
        url: str,
    ) -> None:
        """
        Verify that the uploaded file is publicly reachable.

        Also checks that the returned Content-Type looks valid
        for common media files.
        """

        try:
            response = httpx.get(
                url,
                follow_redirects=True,
                timeout=self.timeout,
            )

            response.raise_for_status()

        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Uploaded file is not publicly reachable: {url}"
            ) from exc

        content_length = len(response.content)

        if content_length == 0:
            raise RuntimeError(
                f"Public file is empty: {url}"
            )

        content_type = response.headers.get(
            "content-type",
            "",
        ).lower()

        guessed_type, _ = mimetypes.guess_type(url)

        if (
            guessed_type
            and content_type
            and guessed_type.split("/")[0]
            != content_type.split("/")[0]
        ):
            raise RuntimeError(
                "Unexpected Content-Type for uploaded file. "
                f"URL: {url}, "
                f"expected approximately: {guessed_type}, "
                f"received: {content_type}"
            )

    def _create_ssh_client(
        self,
    ) -> paramiko.SSHClient:
        client = paramiko.SSHClient()

        client.load_system_host_keys()

        client.set_missing_host_key_policy(
            paramiko.AutoAddPolicy()
        )

        connect_kwargs: dict[str, object] = {
            "hostname": self.ssh_host,
            "port": self.ssh_port,
            "username": self.ssh_user,
            "timeout": self.timeout,
            "banner_timeout": self.timeout,
            "auth_timeout": self.timeout,
        }

        if self.ssh_key_path:
            connect_kwargs["key_filename"] = self.ssh_key_path

        if self.ssh_password:
            connect_kwargs["password"] = self.ssh_password

        client.connect(
            **connect_kwargs,
        )

        return client

    @staticmethod
    def _normalize_remote_path(
        remote_path: str,
    ) -> str:
        remote_path = remote_path.strip()

        if not remote_path:
            raise ValueError(
                "remote_path cannot be empty."
            )

        path = PurePosixPath(
            remote_path.lstrip("/")
        )

        if ".." in path.parts:
            raise ValueError(
                "remote_path cannot contain '..'."
            )

        normalized = str(path)

        if normalized in {"", "."}:
            raise ValueError(
                "Invalid remote_path."
            )

        return normalized

    @staticmethod
    def _mkdir_recursive(
        *,
        sftp: paramiko.SFTPClient,
        remote_directory: str,
    ) -> None:
        """
        mkdir -p equivalent for SFTP.
        """

        path = PurePosixPath(remote_directory)

        current = PurePosixPath("/")

        for part in path.parts:
            if part == "/":
                continue

            current = current / part
            current_str = str(current)

            try:
                sftp.stat(current_str)

            except FileNotFoundError:
                sftp.mkdir(current_str)