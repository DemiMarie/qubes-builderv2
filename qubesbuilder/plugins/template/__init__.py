# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2022 Frédéric Pierret (fepitre) <frederic@invisiblethingslab.com>
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

import datetime
import os
import shutil
import subprocess
from pathlib import Path

import dateutil.parser
import yaml
from dateutil.parser import parse as parsedate

from qubesbuilder.executors import Executor, ExecutorError
from qubesbuilder.log import get_logger
from qubesbuilder.plugins import (
    BasePlugin,
    PluginError,
    BUILD_DIR,
    CACHE_DIR,
    REPOSITORY_DIR,
    PLUGINS_DIR,
)
from qubesbuilder.plugins.publish_rpm import MIN_AGE_DAYS
from qubesbuilder.template import QubesTemplate

log = get_logger("template")

TEMPLATE_VERSION = "4.1.0"
TEMPLATE_REPOSITORIES = [
    "templates-itl-testing",
    "templates-community-testing",
    "templates-itl",
    "templates-community",
]


class TemplateError(PluginError):
    pass


class TemplatePlugin(BasePlugin):
    """
    TemplatePlugin manages distribution build.
    """

    plugin_dependencies = ["source_rpm", "source_deb"]

    def __init__(
        self,
        template: QubesTemplate,
        executor: Executor,
        plugins_dir: Path,
        artifacts_dir: Path,
        qubes_release: str,
        gpg_client: str,
        sign_key: dict,
        repository_publish: dict,
        remote_host: str,
        verbose: bool = False,
        debug: bool = False,
        use_qubes_repo: dict = None,
    ):
        super().__init__(
            dist=template.distribution,
            plugins_dir=plugins_dir,
            artifacts_dir=artifacts_dir,
            verbose=verbose,
            debug=debug,
        )
        self.template = template
        self.executor = executor
        self.qubes_release = qubes_release
        self.gpg_client = gpg_client
        self.sign_key = sign_key
        self.repository_publish = repository_publish
        self.use_qubes_repo = use_qubes_repo or {}
        self.remote_host = remote_host

        self.environment.update(
            {
                "DIST_CODENAME": self.dist.name,  # DIST
                "DIST_NAME": self.dist.fullname,  # DISTRIBUTION
                "DIST_VER": self.dist.version,
                "TEMPLATE_NAME": self.template.name,
                "TEMPLATE_VERSION": TEMPLATE_VERSION,
                "TEMPLATE_FLAVOR": self.template.flavor,
                "TEMPLATE_OPTIONS": " ".join(self.template.options),
                "INSTALL_DIR": "/builder/mnt",
                "ARTIFACTS_DIR": BUILD_DIR,
                "PLUGINS_DIR": PLUGINS_DIR,
                "PACKAGES_DIR": REPOSITORY_DIR,
                "CACHE_DIR": CACHE_DIR / f"cache_{self.dist.name}",
                "USE_QUBES_REPO_VERSION": self.use_qubes_repo.get("version", None),
                "USE_QUBES_REPO_TESTING": 1
                if self.use_qubes_repo.get("testing", None)
                else 0,
            }
        )

    def get_template_timestamp(self):
        # Read information from build stage
        if not (
            self.get_templates_dir() / f"build_timestamp_{self.template.name}"
        ).exists():
            raise TemplateError(f"{self.template}: Cannot find build timestamp.")
        with open(
            self.get_templates_dir() / f"build_timestamp_{self.template.name}"
        ) as f:
            data = f.read().splitlines()

        try:
            template_timestamp = parsedate(data[0]).strftime("%Y%m%d%H%MZ")
        except (dateutil.parser.ParserError, IndexError) as e:
            msg = f"{self.template}: Failed to parse build timestamp format."
            raise TemplateError(msg) from e
        return template_timestamp

    def get_sign_key(self):
        # Check if we have a signing key provided
        sign_key = self.sign_key.get(self.dist.distribution, None) or self.sign_key.get(
            "rpm", None
        )

        if not sign_key:
            raise TemplateError(f"{self.template}: No signing key found.")

        # Check if we have a gpg client provided
        if not self.gpg_client:
            raise TemplateError(f"{self.template}: Please specify GPG client to use!")

        return sign_key

    def upload_to_repository(self):
        if not self.remote_host:
            raise TemplateError(f"{self.template}: Cannot find remote host.")
        try:
            cmd = [
                "rsync",
                "-av",
                "--progress",
                f"{self.get_repository_publish_dir()}/rpm/",  # Pay attention to latest "/", we use rsync!
                {self.remote_host},
            ]
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            raise TemplateError(
                f"{self.template}:{self.dist}: Failed to upload to '{self.remote_host}'."
            ) from e

    def run(
        self, stage: str, repository_publish: str = None, ignore_min_age: bool = False
    ):
        # Update parameters
        self.update_parameters()

        repository_dir = self.get_repository_dir() / self.dist.distribution
        template_artifacts_dir = self.get_templates_dir()

        prepared_images = template_artifacts_dir / "prepared_images"
        qubeized_image = template_artifacts_dir / "qubeized_images" / self.template.name

        repository_dir.mkdir(parents=True, exist_ok=True)
        prepared_images.mkdir(parents=True, exist_ok=True)
        qubeized_image.mkdir(parents=True, exist_ok=True)

        if stage == "prep":
            template_timestamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%MZ")
            self.environment.update({"TEMPLATE_TIMESTAMP": template_timestamp})

            copy_in = [
                (self.plugins_dir / "template", PLUGINS_DIR),
                (repository_dir, REPOSITORY_DIR),
            ] + [
                (self.plugins_dir / plugin, PLUGINS_DIR)
                for plugin in self.plugin_dependencies
            ]

            copy_out = [
                (
                    BUILD_DIR / "prepared_images" / f"{self.template.name}.img",
                    prepared_images,
                ),
            ]
            cmd = [f"make -C {PLUGINS_DIR}/template prepare build-rootimg-prepare"]
            try:
                self.executor.run(cmd, copy_in, copy_out, environment=self.environment)
            except ExecutorError as e:
                msg = f"{self.template}: Failed to build template."
                raise TemplateError(msg) from e

            with open(
                template_artifacts_dir / f"build_timestamp_{self.template.name}", "w"
            ) as f:
                f.write(template_timestamp)

        if stage == "build":
            template_timestamp = self.get_template_timestamp()
            self.environment.update({"TEMPLATE_TIMESTAMP": template_timestamp})

            rpm_fn = f"qubes-template-{self.template.name}-{TEMPLATE_VERSION}-{template_timestamp}.noarch.rpm"

            copy_in = [
                (self.plugins_dir / "template", PLUGINS_DIR),
                (repository_dir, REPOSITORY_DIR),
                (
                    prepared_images / f"{self.template.name}.img",
                    BUILD_DIR / "prepared_images",
                ),
            ] + [
                (self.plugins_dir / plugin, PLUGINS_DIR)
                for plugin in self.plugin_dependencies
            ]

            # Copy-in previously prepared base root img
            copy_out = [
                (
                    BUILD_DIR / "qubeized_images" / self.template.name / "root.img",
                    qubeized_image,
                ),
                (
                    BUILD_DIR / f"rpmbuild/RPMS/noarch/{rpm_fn}",
                    template_artifacts_dir / "rpm",
                ),
            ]
            cmd = [f"make -C {PLUGINS_DIR}/template prepare prepare build"]
            try:
                self.executor.run(cmd, copy_in, copy_out, environment=self.environment)
            except ExecutorError as e:
                msg = f"{self.template}: Failed to build template."
                raise TemplateError(msg) from e

            with open(
                template_artifacts_dir / f"build_timestamp_{self.template.name}", "w"
            ) as f:
                f.write(template_timestamp)

        # Sign stage for templates
        if stage == "sign":
            db_path = template_artifacts_dir / "rpmdb"
            # We ensure to create a clean keyring for RPM
            if db_path.exists():
                shutil.rmtree(db_path)

            sign_key = self.get_sign_key()
            sign_key_asc = template_artifacts_dir / f"{sign_key}.asc"
            cmd = [
                f"mkdir -p {db_path}",
                f"{self.gpg_client} --armor --export {sign_key} > {sign_key_asc}",
                f"rpmkeys --dbpath={db_path} --import {sign_key_asc}",
            ]
            try:
                self.executor.run(cmd)
            except ExecutorError as e:
                msg = f"{self.template}: Failed to create RPM dbpath."
                raise TemplateError(msg) from e

            template_timestamp = self.get_template_timestamp()

            rpm = (
                template_artifacts_dir
                / "rpm"
                / f"qubes-template-{self.template.name}-{TEMPLATE_VERSION}-{template_timestamp}.noarch.rpm"
            )
            if not rpm.exists():
                msg = f"{self.template}: Cannot find template RPM '{rpm}'."
                raise TemplateError(msg)

            try:
                log.info(f"{self.template}: Signing '{rpm.name}'.")
                cmd = [
                    f"{self.plugins_dir}/sign_rpm/scripts/sign-rpm "
                    f"--sign-key {sign_key} --db-path {db_path} --rpm {rpm}"
                ]
                self.executor.run(cmd)
            except ExecutorError as e:
                msg = f"{self.template}: Failed to sign template RPM '{rpm}'."
                raise TemplateError(msg) from e

        # Publish stage for template components
        if stage == "publish":
            # repository-publish directory
            artifacts_dir = self.get_repository_publish_dir() / "rpm"

            # Check if publish repository is valid
            if not repository_publish:
                repository_publish = self.repository_publish.get(
                    "templates", "current-testing"
                )

            if repository_publish not in TEMPLATE_REPOSITORIES:
                raise TemplateError(
                    f"Invalid repository for template: '{repository_publish}'"
                )

            if repository_publish in ("templates-itl", "templates-community"):
                failure_msg = (
                    f"{self.template}: "
                    f"Refusing to publish to '{repository_publish}' as template is not uploaded "
                    f"to '{repository_publish}-testing' for at least {MIN_AGE_DAYS} days."
                )
                # Check template is published in testing
                if not (
                    template_artifacts_dir / f"{self.template.name}_publish_info.yml"
                ).exists():
                    raise TemplateError(failure_msg)
                else:
                    # Get existing publish info
                    with open(
                        template_artifacts_dir
                        / f"{self.template.name}_publish_info.yml"
                    ) as f:
                        publish_info = yaml.safe_load(f.read())
                    # Check for valid repositories under which packages are published
                    if (
                        publish_info.get("repository-publish", None)
                        not in TEMPLATE_REPOSITORIES
                    ):
                        raise TemplateError(failure_msg)
                    if publish_info["repository-publish"] == repository_publish:
                        log.info(
                            f"{self.template}: Already published to '{repository_publish}'."
                        )
                        return

                    # Check minimum day that packages are available for testing
                    publish_date = datetime.datetime.utcfromtimestamp(
                        os.stat(
                            template_artifacts_dir
                            / f"{self.template.name}_publish_info.yml"
                        ).st_mtime
                    )
                    # Check that packages have been published before threshold_date
                    threshold_date = datetime.datetime.utcnow() - datetime.timedelta(
                        days=MIN_AGE_DAYS
                    )
                    if not ignore_min_age and publish_date > threshold_date:
                        raise TemplateError(failure_msg)

            # Ensure dbpath from sign stage (still) exists
            db_path = template_artifacts_dir / "rpmdb"
            if not db_path.exists():
                msg = f"{self.template}: Failed to find RPM DB path."
                raise TemplateError(msg)

            # Create publish repository skeleton with at least underlying
            # template distribution
            comps = (
                self.plugins_dir
                / f"publish_rpm/comps/comps-{self.dist.package_set}.xml"
            )
            create_skeleton_cmd = [
                f"{self.plugins_dir}/publish_rpm/scripts/create-skeleton",
                self.qubes_release,
                self.dist.package_set,
                self.dist.name,
                str(artifacts_dir.absolute()),
                str(comps.absolute()),
            ]
            cmd = [" ".join(create_skeleton_cmd)]
            try:
                self.executor.run(cmd)
            except ExecutorError as e:
                msg = f"{self.template}: Failed to create repository skeleton."
                raise TemplateError(msg) from e

            # Read information from build stage
            with open(
                template_artifacts_dir / f"build_timestamp_{self.template.name}"
            ) as f:
                data = f.read().splitlines()

            try:
                timestamp = parsedate(data[0]).strftime("%Y%m%d%H%MZ")
            except (dateutil.parser.ParserError, IndexError) as e:
                msg = f"{self.template}: Failed to parse build timestamp format."
                raise TemplateError(msg) from e

            rpm = (
                template_artifacts_dir
                / "rpm"
                / f"qubes-template-{self.template.name}-{TEMPLATE_VERSION}-{timestamp}.noarch.rpm"
            )
            if not rpm.exists():
                msg = f"{self.template}: Cannot find template RPM '{rpm}'."
                raise TemplateError(msg)

            # We check that signature exists (--check-only option)
            log.info(f"{self.template}: Verifying signatures.")
            sign_key = self.get_sign_key()
            try:
                cmd = [
                    f"{self.plugins_dir}/sign_rpm/scripts/sign-rpm "
                    f"--sign-key {sign_key} --db-path {db_path} --rpm {rpm} --check-only"
                ]
                self.executor.run(cmd)
            except ExecutorError as e:
                msg = f"{self.template}: Failed to check signatures."
                raise TemplateError(msg) from e

            # Publish packages with hardlinks to built RPMs
            log.info(f"{self.template}: Publishing RPMs.")
            target_dir = artifacts_dir / f"{self.qubes_release}/{repository_publish}"
            try:
                target_path = target_dir / "rpm" / rpm.name
                target_path.unlink(missing_ok=True)
                # target_path.hardlink_to(rpm)
                os.link(rpm, target_path)
            except (ValueError, PermissionError, NotImplementedError) as e:
                msg = f"{self.template}: Failed to publish packages."
                raise TemplateError(msg) from e

            # Createrepo published templates
            log.info(f"{self.template}: Updating metadata.")
            cmd = [f"cd {target_dir}", "createrepo_c ."]
            try:
                shutil.rmtree(target_dir / "repodata")
                self.executor.run(cmd)
            except (ExecutorError, OSError) as e:
                msg = f"{self.template}: Failed to 'createrepo_c'"
                raise TemplateError(msg) from e

            # Sign metadata
            log.info(f"{self.template}: Signing metadata.")
            repomd = target_dir / "repodata/repomd.xml"
            cmd = [
                f"{self.gpg_client} --batch --no-tty --yes --detach-sign --armor -u {sign_key} {repomd} > {repomd}.asc",
            ]
            try:
                self.executor.run(cmd)
            except (ExecutorError, OSError) as e:
                msg = f"{self.template}: Failed to sign metadata"
                raise TemplateError(msg) from e

            # Save package information we published for committing into current
            template_artifacts_dir.mkdir(parents=True, exist_ok=True)
            try:
                with open(
                    template_artifacts_dir / f"{self.template.name}_publish_info.yml",
                    "w",
                ) as f:
                    info = {
                        "repository-publish": repository_publish,
                        "timestamp": timestamp,
                    }
                    f.write(yaml.safe_dump(info))
            except (PermissionError, yaml.YAMLError) as e:
                msg = f"{self.template}: Failed to write publish info."
                raise TemplateError(msg) from e
