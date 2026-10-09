"""Publishing media to the public HTTPS storage.

Two modes, selected by STORAGE_MODE:

* ``local`` -- the storage directory is bind-mounted into this process.
  Used on the VPS, where the storage and the worker sit on the same host:
  writing a file directly removes the SSH key, the root login and the
  network round trip that SFTP would need.
* ``sftp`` -- the historical mode, kept for a storage on another host and
  for existing scripts. Unchanged behaviour.

Both modes return the same public HTTPS URL and verify that the file is
really reachable from outside before reporting success.
"""
from __future__ import annotations

import mimetypes
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final

import httpx
from dotenv import load_dotenv


if TYPE_CHECKING:
    import paramiko


load_dotenv()

MODE_LOCAL: Final[str] = "local"
MODE_SFTP: Final[str] = "sftp"


class StorageUploader:
    """
    Publishes a file to the public storage and returns its HTTPS URL.

    Example:

        uploader = StorageUploader()

        url = uploader.upload(
            local_path="artifacts/post-image.jpg",
            remote_path="ai-smm/threads/post-image.jpg",
        )

    ``remote_path`` is always relative to the root of the public base URL,
    in both modes, so callers do not need to know which mode is active.

    In local mode the process is given write access to exactly one
    directory, which corresponds to STORAGE_LOCAL_PREFIX under the public
    base URL. A remote_path outside that prefix is refused: the uploader
    cannot touch another project's media even if asked to.
    """

    # No default host or root: a stale default silently uploaded to the
    # decommissioned Beget storage while reporting success. Both values are
    # now required, so a misconfiguration fails loudly instead.
    DEFAULT_SSH_PORT: Final[int] = 22

    #: Mode 644: the file is served by a web server running as another
    #: user, so it must be world-readable.
    PUBLISHED_FILE_MODE: Final[int] = 0o644

    def __init__(
        self,
        *,
        mode: str | None = None,
        local_root: str | Path | None = None,
        local_prefix: str | None = None,
        ssh_host: str | None = None,
        ssh_port: int | None = None,
        ssh_user: str | None = None,
        ssh_key_path: str | None = None,
        ssh_password: str | None = None,
        remote_root: str | None = None,
        public_base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        # Default stays sftp so existing scripts and .env files keep
        # working without being edited.
        self.mode = (
            mode or os.getenv("STORAGE_MODE") or MODE_SFTP
        ).strip().lower()

        self.local_root = (
            Path(local_root).expanduser()
            if local_root
            else Path(os.getenv("STORAGE_LOCAL_ROOT", "")).expanduser()
            if os.getenv("STORAGE_LOCAL_ROOT")
            else None
        )

        self.local_prefix = (
            local_prefix
            if local_prefix is not None
            else os.getenv("STORAGE_LOCAL_PREFIX", "")
        ).strip("/")

        self.ssh_host = ssh_host or os.getenv("STORAGE_SSH_HOST", "")
        self.ssh_port = ssh_port or int(
            os.getenv("STORAGE_SSH_PORT", str(self.DEFAULT_SSH_PORT))
        )
        self.ssh_user = ssh_user or os.getenv("STORAGE_SSH_USER", "")
        self.ssh_key_path = ssh_key_path or os.getenv("STORAGE_SSH_KEY_PATH")
        self.ssh_password = ssh_password or os.getenv("STORAGE_SSH_PASSWORD")

        self.remote_root = (
            remote_root or os.getenv("STORAGE_REMOTE_ROOT") or ""
        ).rstrip("/")

        self.public_base_url = (
            public_base_url or os.getenv("STORAGE_PUBLIC_BASE_URL") or ""
        ).rstrip("/")

        self.timeout = timeout

        self._validate_config()

    def _validate_config(self) -> None:
        if self.mode not in {MODE_LOCAL, MODE_SFTP}:
            raise ValueError(
                f"STORAGE_MODE must be {MODE_LOCAL!r} or {MODE_SFTP!r}, "
                f"got {self.mode!r}."
            )

        if not self.public_base_url:
            raise ValueError(
                "STORAGE_PUBLIC_BASE_URL is not configured."
            )

        if not self.public_base_url.startswith("https://"):
            raise ValueError(
                "STORAGE_PUBLIC_BASE_URL must be an https:// URL; Threads "
                "refuses to fetch media over plain HTTP."
            )

        if self.mode == MODE_LOCAL:
            self._validate_local_config()

            return

        self._validate_sftp_config()

    def _validate_local_config(self) -> None:
        if self.local_root is None:
            raise ValueError(
                "STORAGE_LOCAL_ROOT is not configured; local mode needs the "
                "directory the public storage is mounted at."
            )

        if not self.local_prefix:
            raise ValueError(
                "STORAGE_LOCAL_PREFIX is not configured; local mode needs "
                "the path STORAGE_LOCAL_ROOT corresponds to under "
                "STORAGE_PUBLIC_BASE_URL, for example 'ai-smm/threads'."
            )

        if ".." in PurePosixPath(self.local_prefix).parts:
            raise ValueError(
                "STORAGE_LOCAL_PREFIX cannot contain '..'."
            )

        self.local_root = self.local_root.resolve()

        if not self.local_root.is_dir():
            raise FileNotFoundError(
                f"STORAGE_LOCAL_ROOT is not a directory: {self.local_root}"
            )

        if not os.access(self.local_root, os.W_OK | os.X_OK):
            raise PermissionError(
                f"STORAGE_LOCAL_ROOT is not writable: {self.local_root}. "
                "Grant the runtime user write access to exactly this "
                "directory, and nothing above it."
            )

    def _validate_sftp_config(self) -> None:
        if not self.ssh_host:
            raise ValueError(
                "STORAGE_SSH_HOST is not configured."
            )

        if not self.ssh_user:
            raise ValueError(
                "STORAGE_SSH_USER is not configured."
            )

        if not self.remote_root:
            raise ValueError(
                "STORAGE_REMOTE_ROOT is not configured."
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

        if self.mode == MODE_LOCAL:
            self._write_local(
                content=local_file.read_bytes(),
                normalized_remote_path=normalized_remote_path,
            )
        else:
            self._put_sftp(
                local_file=local_file,
                normalized_remote_path=normalized_remote_path,
            )

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

        if self.mode == MODE_LOCAL:
            self._write_local(
                content=content,
                normalized_remote_path=normalized_remote_path,
            )
        else:
            self._put_sftp_bytes(
                content=content,
                normalized_remote_path=normalized_remote_path,
            )

        public_url = self.build_public_url(
            normalized_remote_path
        )

        if verify_public_url:
            self.verify_url(public_url)

        return public_url

    # -- local mode -------------------------------------------------------

    def _resolve_local_target(
        self,
        normalized_remote_path: str,
    ) -> Path:
        """Map a public path onto a file inside the one writable directory.

        The prefix check is the containment guarantee: the process is
        mounted at exactly one directory, and a remote_path belonging to a
        different prefix is refused rather than silently redirected.
        """

        assert self.local_root is not None

        path = PurePosixPath(normalized_remote_path)
        prefix = PurePosixPath(self.local_prefix)

        if not path.is_relative_to(prefix):
            raise ValueError(
                f"remote_path {normalized_remote_path!r} is outside the "
                f"writable prefix {self.local_prefix!r}; local mode cannot "
                "write there."
            )

        relative = path.relative_to(prefix)

        if not relative.parts:
            raise ValueError(
                "remote_path must name a file, not the prefix itself."
            )

        target = (self.local_root / Path(*relative.parts)).resolve()

        # resolve() has followed any symlink; re-check so a link planted
        # inside the directory cannot be used to escape it.
        if not target.is_relative_to(self.local_root):
            raise ValueError(
                f"remote_path {normalized_remote_path!r} resolves outside "
                f"{self.local_root}."
            )

        return target

    def _write_local(
        self,
        *,
        content: bytes,
        normalized_remote_path: str,
    ) -> Path:
        """Publish bytes atomically.

        Meta fetches the URL moments after the post is created, so a reader
        must never see a half-written file: the bytes land in a temporary
        file in the same directory and are moved into place with a rename,
        which is atomic within one filesystem.
        """

        target = self._resolve_local_target(normalized_remote_path)

        target.parent.mkdir(parents=True, exist_ok=True)

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".part",
            dir=target.parent,
        )
        temporary_path = Path(temporary_name)

        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())

            # mkstemp creates the file 0600; the web server runs as another
            # user and has to be able to read it.
            os.chmod(temporary_path, self.PUBLISHED_FILE_MODE)

            os.replace(temporary_path, target)

            directory = os.open(target.parent, os.O_RDONLY)

            try:
                os.fsync(directory)
            finally:
                os.close(directory)

        finally:
            if temporary_path.exists():
                temporary_path.unlink()

        return target

    # -- sftp mode --------------------------------------------------------

    def _remote_paths(
        self,
        normalized_remote_path: str,
    ) -> tuple[str, str]:
        remote_file = str(
            PurePosixPath(self.remote_root)
            / PurePosixPath(normalized_remote_path)
        )

        return remote_file, str(PurePosixPath(remote_file).parent)

    def _put_sftp(
        self,
        *,
        local_file: Path,
        normalized_remote_path: str,
    ) -> None:
        remote_file, remote_directory = self._remote_paths(
            normalized_remote_path
        )

        ssh_client = self._create_ssh_client()

        try:
            sftp = ssh_client.open_sftp()

            try:
                self._mkdir_recursive(
                    sftp=sftp,
                    remote_directory=remote_directory,
                )

                sftp.put(str(local_file), remote_file)
                sftp.chmod(remote_file, self.PUBLISHED_FILE_MODE)

            finally:
                sftp.close()

        finally:
            ssh_client.close()

    def _put_sftp_bytes(
        self,
        *,
        content: bytes,
        normalized_remote_path: str,
    ) -> None:
        remote_file, remote_directory = self._remote_paths(
            normalized_remote_path
        )

        ssh_client = self._create_ssh_client()

        try:
            sftp = ssh_client.open_sftp()

            try:
                self._mkdir_recursive(
                    sftp=sftp,
                    remote_directory=remote_directory,
                )

                with sftp.file(remote_file, mode="wb") as remote_handle:
                    remote_handle.write(content)

                sftp.chmod(remote_file, self.PUBLISHED_FILE_MODE)

            finally:
                sftp.close()

        finally:
            ssh_client.close()

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

    def _create_ssh_client(self) -> paramiko.SSHClient:
        import paramiko

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