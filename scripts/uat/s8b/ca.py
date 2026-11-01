"""A throw-away test CA and a leaf certificate for chosen host names, made with the ``openssl`` command line.

Dev/test only: nothing here is a runtime dependency of the application. ``openssl`` must be on PATH (Git for Windows
ships one; every CI image has one).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class TestCA:
    __test__ = False  # not a pytest class

    def __init__(self, directory: Path, hosts: list[str], openssl: str | None = None) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        git_openssl = r"C:\Program Files\Git\usr\bin\openssl.exe"
        self.openssl = openssl or shutil.which("openssl") or (git_openssl if Path(git_openssl).is_file() else None)
        self.hosts = list(hosts)
        self.ca_pem = self.dir / "ca.pem"
        self.ca_key = self.dir / "ca.key"
        self.leaf_pem = self.dir / "leaf.pem"  # leaf certificate followed by nothing else (the CA is the trust anchor)
        self.leaf_key = self.dir / "leaf.key"
        if all(p.is_file() for p in (self.ca_pem, self.ca_key, self.leaf_pem, self.leaf_key)):
            return  # made earlier (on the host, for Windows Sandbox, which has no openssl): reuse, do not regenerate
        self._make()

    def _run(self, *args: str) -> None:
        subprocess.run([str(self.openssl), *args], check=True, capture_output=True, text=True, cwd=self.dir)

    def _make(self) -> None:
        if not self.openssl:
            raise RuntimeError("openssl not found on PATH")
        ec = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
        self._run(
            "req",
            "-x509",
            *ec,
            "-keyout",
            str(self.ca_key),
            "-out",
            str(self.ca_pem),
            "-days",
            "30",
            "-subj",
            "/CN=faultproxy test CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-addext",
            "subjectKeyIdentifier=hash",
        )
        san = ",".join(f"DNS:{h}" for h in self.hosts)
        ext = self.dir / "leaf.ext"
        ext.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectKeyIdentifier=hash\n"
            "authorityKeyIdentifier=keyid\n"
            f"subjectAltName={san}\n"
        )
        csr = self.dir / "leaf.csr"
        self._run("req", "-new", *ec, "-keyout", str(self.leaf_key), "-out", str(csr), "-subj", f"/CN={self.hosts[0]}")
        self._run(
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(self.ca_pem),
            "-CAkey",
            str(self.ca_key),
            "-CAcreateserial",
            "-out",
            str(self.leaf_pem),
            "-days",
            "30",
            "-extfile",
            str(ext),
        )
