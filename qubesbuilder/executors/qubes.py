# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2021 Frédéric Pierret (fepitre) <frederic@invisiblethingslab.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePath
from shlex import quote
from typing import List, Tuple, Union

from qubesbuilder.common import sanitize_line, PROJECT_PATH
from qubesbuilder.executors import Executor, ExecutorError


# From https://github.com/QubesOS/qubes-core-admin-client/blob/main/qubesadmin/utils.py#L159-L173
def encode_for_vmexec(input_string):
    def encode(part):
        if part.group(0) == b"-":
            return b"--"
        return "-{:02X}".format(ord(part.group(0))).encode("ascii")

    part = re.sub(rb"[^a-zA-Z0-9_.]", encode, input_string.encode("utf-8"))

    return part.decode("ascii")


def quote_list(args: List[Union[str, Path]]) -> str:
    return " ".join(map(lambda x: quote(str(x)), args))


def quote_and_list(cmds: List[List[Union[str, Path]]]) -> str:
    return " && ".join(map(quote_list, cmds))


def build_run_cmd(vm_name: str, cmd: List[Union[str, Path]]) -> List[str]:
    return ["/usr/bin/qvm-run-vm", "--", vm_name, quote_list(cmd)]


def build_run_cmd_and_list(
    vm_name: str, cmds: List[List[Union[str, Path]]]
) -> List[str]:
    return ["/usr/bin/qvm-run-vm", "--", vm_name, quote_and_list(cmds)]


class QubesExecutor(Executor):
    def __init__(self, dispvm: str = "dom0", **kwargs):
        super().__init__(**kwargs)
        if dispvm == "@dispvm":
            self._dispvm_template = "dom0"
        else:
            self._dispvm_template = dispvm

    def get_user(self):
        return "user"

    def get_group(self):
        return "user"

    def copy_in(self, vm: str, source_path: Path, destination_dir: PurePath):  # type: ignore
        src = source_path.expanduser().resolve()
        dst = destination_dir
        encoded_dst_path = encode_for_vmexec(str((dst / src.name).as_posix()))
        copy_in_cmd = [
            "/usr/lib/qubes/qrexec-client-vm",
            "--",
            vm,
            f"qubesbuilder.FileCopyIn+{encoded_dst_path}",
            "/usr/lib/qubes/qfile-agent",
            str(src),
        ]
        try:
            self.log.debug(f"copy-in (cmd): {' '.join(copy_in_cmd)}")
            subprocess.run(copy_in_cmd, check=True)
        except subprocess.CalledProcessError as e:
            if e.stderr is not None:
                content = sanitize_line(e.stderr.rstrip(b"\n")).rstrip()
            else:
                content = str(e)
            msg = f"Failed to copy-in: {content}"
            raise ExecutorError(msg, name=vm)

    def copy_out(
        self,
        vm: str,
        source_path: PurePath,
        destination_dir: Path,
        dig_holes=False,
    ):  # type: ignore
        src = source_path
        dst = destination_dir.resolve()

        # Remove local file or directory if exists
        dst_path = dst / src.name
        if os.path.exists(dst_path):
            if dst_path.is_dir():
                shutil.rmtree(dst / src.name)
            else:
                os.remove(dst_path)

        dst.mkdir(parents=True, exist_ok=True)

        old_unpacker_path = "/usr/lib/qubes/qfile-unpacker"
        new_unpacker_path = "/usr/bin/qfile-unpacker"
        if os.path.exists(new_unpacker_path):
            unpacker_path = new_unpacker_path
        else:
            unpacker_path = old_unpacker_path
        encoded_src_path = encode_for_vmexec(str(src))
        cmd = [
            "/usr/lib/qubes/qrexec-client-vm",
            "--filter-escape-chars-stderr",
            "--filter-escape-chars-stdout",
            vm,
            f"qubesbuilder.FileCopyOut+{encoded_src_path}",
            unpacker_path,
            str(os.getuid()),
            str(dst),
        ]
        try:
            self.log.debug(f"copy-out (cmd): {' '.join(cmd)}")
            subprocess.run(cmd, check=True)

            if dig_holes and not dst_path.is_dir():
                self.log.debug(
                    "copy-out (detect zeroes and replace with holes)"
                )
                subprocess.run(
                    ["/usr/bin/fallocate", "-d", str(dst_path)], check=True
                )
        except subprocess.CalledProcessError as e:
            if e.stderr is not None:
                content = sanitize_line(e.stderr.rstrip(b"\n")).rstrip()
            else:
                content = str(e)
            msg = f"Failed to copy-out: {content}"
            raise ExecutorError(msg, name=vm)


class LinuxQubesExecutor(QubesExecutor):
    def __init__(
        self, dispvm: str = "dom0", clean: Union[str, bool] = True, **kwargs
    ):
        super().__init__(dispvm=dispvm, clean=clean, **kwargs)

    @staticmethod
    def cleanup(dispvm):
        # Kill the DispVM to prevent hanging for while
        subprocess.run(
            ["qrexec-client-vm", "--", dispvm, "admin.vm.Kill"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )

    def run(  # type: ignore
        self,
        cmd: List[str],
        copy_in: List[Tuple[Path, PurePath]] = None,
        copy_out: List[Tuple[PurePath, Path]] = None,
        files_inside_executor_with_placeholders: List[Path] = None,
        environment: dict = None,
        no_fail_copy_out_allowed_patterns=None,
        dig_holes: bool = False,
    ):
        dispvm = None
        try:
            result = subprocess.run(
                [
                    "qrexec-client-vm",
                    "--",
                    self._dispvm_template,
                    "admin.vm.CreateDisposable",
                ],
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )
            stdout = result.stdout
            if not stdout.startswith(b"0\x00"):
                raise ExecutorError("Failed to create disposable qube.")
            stdout = stdout[2:]
            if not re.match(rb"\Adisp(0|[1-9][0-9]{0,8})\Z", stdout):
                raise ExecutorError("Failed to create disposable qube.")
            try:
                dispvm = stdout.decode("ascii", "strict")
            except UnicodeDecodeError as e:
                raise ExecutorError(
                    f"Failed to obtain disposable qube name: {str(e)}"
                )

            # Start the DispVM
            subprocess.run(
                [
                    "/usr/lib/qubes/qrexec-client-vm",
                    dispvm,
                    "admin.vm.Start",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
            )

            # Copy qubes-builder RPC
            copy_rpc_cmd = [
                "/usr/lib/qubes/qrexec-client-vm",
                "--filter-escape-chars-stderr",
                dispvm,
                "qubes.Filecopy",
                "/usr/lib/qubes/qfile-agent",
                str(PROJECT_PATH / "rpc" / "qubesbuilder.FileCopyIn"),
                str(PROJECT_PATH / "rpc" / "qubesbuilder.FileCopyOut"),
            ]
            subprocess.run(
                copy_rpc_cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
            )

            prep_cmd = build_run_cmd_and_list(
                dispvm,
                [
                    [
                        "sudo",
                        "mkdir",
                        "-p",
                        "--",
                        str(self.get_builder_dir()),
                        str(self.get_builder_dir() / "build"),
                        str(self.get_builder_dir() / "plugins"),
                        str(self.get_builder_dir() / "distfiles"),
                        "/usr/local/etc/qubes-rpc",
                    ],
                    [
                        "sudo",
                        "mv",
                        "-f",
                        "--",
                        f"/home/{self.get_user()}/QubesIncoming/{os.uname().nodename}/qubesbuilder.FileCopyIn",
                        f"/home/{self.get_user()}/QubesIncoming/{os.uname().nodename}/qubesbuilder.FileCopyOut",
                        "/usr/local/etc/qubes-rpc/",
                    ],
                    [
                        "sudo",
                        "chmod",
                        "+x",
                        "--",
                        "/usr/local/etc/qubes-rpc/qubesbuilder.FileCopyIn",
                        "/usr/local/etc/qubes-rpc/qubesbuilder.FileCopyOut",
                    ],
                    [
                        "sudo",
                        "bash",
                        "-c",
                        "if [ -x /usr/sbin/restorecon ]; then restorecon -R /usr/local/etc/qubes-rpc/; fi;",
                    ],
                    [
                        "sudo",
                        "chown",
                        "-R",
                        "--",
                        f"{self.get_user()}:{self.get_group()}",
                        str(self.get_builder_dir()),
                    ],
                ],
            )
            subprocess.run(prep_cmd, stdin=subprocess.DEVNULL)

            # copy-in hook
            for src_in, dst_in in sorted(
                set(copy_in or []), key=lambda x: x[1]
            ):
                self.copy_in(dispvm, source_path=src_in, destination_dir=dst_in)

            # replace placeholders
            if files_inside_executor_with_placeholders and isinstance(
                files_inside_executor_with_placeholders, list
            ):
                files = [
                    self.replace_placeholders(str(f))
                    for f in files_inside_executor_with_placeholders
                ]
                builder_dir = str(self.get_builder_dir())
                if "@" in builder_dir:
                    raise ExecutorError(
                        f"'@' not permitted in builder directory (got {builder_dir!r})"
                    )
                sed_rhs = (
                    builder_dir.replace("\\", "\\\\")
                    .replace("&", "\\&")
                    .replace("#", "\\#")
                    .replace("\n", "\\\n")
                )
                sed_cmd = build_run_cmd(
                    dispvm,
                    [
                        "sed",
                        "-i",
                        "--",
                        f"s#@BUILDER_DIR@#{sed_rhs}#g",
                    ]
                    + files,
                )
                subprocess.run(sed_cmd, stdin=subprocess.DEVNULL, check=True)

            bash_env = []
            if environment:
                for key, val in environment.items():
                    if "=" in str(key):
                        raise ExecutorError(
                            "Environment variable name cannot contain '='"
                        )
                    bash_env.append(f"{str(key)}={str(val)}")

            qvm_run_cmd = build_run_cmd(
                dispvm,
                [
                    "env",
                    "--",
                    *bash_env,
                    "bash",
                    "-c",
                    " && ".join(cmd),
                ],
            )

            self.log.info(f"Using executor qubes:{dispvm}.")
            self.log.debug(" ".join(qvm_run_cmd))

            # stream output for command
            rc = self.execute(qvm_run_cmd)
            if rc != 0:
                msg = f"Failed to run '{' '.join(qvm_run_cmd)}' (status={rc})."
                raise ExecutorError(msg, name=dispvm)

            # copy-out hook
            for src_out, dst_out in sorted(
                set(copy_out or []), key=lambda x: x[1]
            ):
                try:
                    self.copy_out(
                        dispvm,
                        source_path=src_out,
                        destination_dir=dst_out,
                        dig_holes=dig_holes,
                    )
                except ExecutorError as e:
                    # Ignore copy-out failure if requested
                    if isinstance(
                        no_fail_copy_out_allowed_patterns, list
                    ) and any(
                        [
                            p in src_out.name
                            for p in no_fail_copy_out_allowed_patterns
                        ]
                    ):
                        self.log.debug(
                            f"File not found inside container: {src_out}."
                        )
                        continue
                    raise e
        except (subprocess.CalledProcessError, ExecutorError) as e:
            if dispvm and self._clean_on_error:
                self.cleanup(dispvm)
            raise e
        else:
            if dispvm and self._clean:
                self.cleanup(dispvm)
