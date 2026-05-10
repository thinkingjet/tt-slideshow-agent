from __future__ import annotations

import shutil
from pathlib import Path


class LocalStorage:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def ensure(self) -> None:
        for directory in (self.raw_dir, self.cta_dir, self.outputs_dir, self.tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def cta_dir(self) -> Path:
        return self.root / "cta"

    @property
    def outputs_dir(self) -> Path:
        return self.root / "outputs"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    def job_raw_dir(self, job_id: str) -> Path:
        path = self.raw_dir / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def job_output_dir(self, job_id: str) -> Path:
        path = self.outputs_dir / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def job_tmp_dir(self, job_id: str) -> Path:
        path = self.tmp_dir / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def copy_cta(self, job_id: str, source_path: Path | str) -> Path:
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"CTA file does not exist: {source}")
        extension = source.suffix or ".mp4"
        destination = self.cta_dir / f"{job_id}{extension}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination

    def raw_output_template(self, job_id: str) -> str:
        return str(self.job_raw_dir(job_id) / "%(id)s.%(ext)s")

    def stitched_output_path(self, job_id: str, youtube_id: str) -> Path:
        return self.job_output_dir(job_id) / f"{youtube_id}_stitched.mp4"

