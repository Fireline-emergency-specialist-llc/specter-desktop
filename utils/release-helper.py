import hashlib
import logging
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


logging.basicConfig(format="%(levelname)s:%(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class Sha256sumFile:
    def __init__(self, name, target_dir="./signing_dir"):
        self.name = name
        self.target_dir = target_dir
        self.hashed_files = {}

    def is_in_target_dir(self):
        return os.path.isfile(os.path.join(self.target_dir, self.name))

    def download_from_tag(self, tag, gc):
        gc.download_artifact(tag, self.name, target_dir=self.target_dir)
        gc.download_artifact(tag, self.name + ".asc", target_dir=self.target_dir)
        self.read()

    def download_hashed_files(self, gc):
        for file in self.hashed_files.keys():
            logger.info(f"Downloading {file} from {tag}")
            gc.download_artifact(self.tag, file, target_dir=self.target_dir)

    def read(self):
        with open(os.path.join(self.target_dir, self.name), "r") as file:
            line = file.readline()
            while line:
                line = line.split(maxsplit=2)
                self.hashed_files[line[1]] = line[0]
                line = file.readline()

    def print(self):
        for hashed_file, hash in self.hashed_files.items():
            print(f"{hash} {hashed_file}")

    def write(self):
        with open(os.path.join(self.target_dir, self.name), "w") as file:
            for hashed_file, hash in self.hashed_files.items():
                file.write(f"{hash} {hashed_file}")

    def add_file(self, file):
        self.hashed_files[file] = Sha256sumFile.sha256_checksum(file, self.target_dir)

    def check_hashes(self):
        returncode = subprocess.call(
            ["sha256sum", "-c", self.name], cwd=self.target_dir
        )
        if returncode != 0:
            raise Exception(
                f"Could not validate hashes for file {self.name}: {subprocess.run(['sha256sum', '-c', self.name], cwd=self.target_dir)}"
            )

    def check_sig(self):
        returncode = subprocess.call(
            ["gpg", "--verify", self.name + ".asc"], cwd=self.target_dir
        )
        if returncode != 0:
            raise Exception(f"Could not validate signature of file {self.name}")

    @classmethod
    def sha256_checksum(cls, filename, folder, block_size=65536):
        sha256 = hashlib.sha256()
        with open(os.path.join(folder, filename), "rb") as f:
            for block in iter(lambda: f.read(block_size), b""):
                sha256.update(block)
        return sha256.hexdigest()


class ReleaseHelper:
    def __init__(self):
        pass

    def init_gitlab(self):
        import gitlab

        if os.environ.get("GITLAB_PRIVATE_TOKEN"):
            logger.info("Using GITLAB_PRIVATE_TOKEN")
            self.gl = gitlab.Gitlab(
                "http://gitlab.com",
                private_token=os.environ.get("GITLAB_PRIVATE_TOKEN"),
            )
        elif os.environ.get("CI_JOB_TOKEN"):
            logger.info("Using CI_JOB_TOKEN")
            self.gl = gitlab.Gitlab(
                "http://gitlab.com", job_token=os.environ["CI_JOB_TOKEN"]
            )
        else:
            raise Exception(
                "Can't authenticate against Gitlab ( export GITLAB_PRIVATE_TOKEN )"
            )

        if os.environ.get("CI_PROJECT_ROOT_NAMESPACE"):
            project_root_namespace = os.environ.get("CI_PROJECT_ROOT_NAMESPACE")
            logger.info(f"Using project_root_namespace: {project_root_namespace}")
        else:
            raise Exception(
                "no CI_PROJECT_ROOT_NAMESPACE given ( export CI_PROJECT_ROOT_NAMESPACE=k9ert )"
            )

        if os.environ.get("CI_PROJECT_ID"):
            self.project_id = os.environ.get("CI_PROJECT_ID")
            self.github_project = f"{project_root_namespace}/specter-desktop"
        else:
            self.project_id = 15721074  # cryptoadvance/specter-desktop
            # self.project_id =
            self.github_project = f"{project_root_namespace}/specter-desktop"

        logger.info(f"Using project_id: {self.project_id}")
        logger.info(f"Using github_project: {self.github_project}")

        self.project = self.gl.projects.get(self.project_id)

        if os.environ.get("CI_COMMIT_TAG"):
            self.tag = os.environ.get("CI_COMMIT_TAG")
        else:
            raise Exception("no tag given ( export CI_COMMIT_TAG=v0.0.0.0-pre13 )")
        logger.info(f"Using tag: {self.tag}")

        if os.environ.get("CI_PIPELINE_ID"):
            pipeline_id = os.environ.get("CI_PIPELINE_ID")
        else:
            logger.info(
                "no CI_PIPELINE_ID given, trying to find an appropriate one ..."
            )
            pipelines = self.project.pipelines.list()
            for pipeline in pipelines:
                if pipeline.ref == self.tag:
                    self.pipeline = pipeline
                    logger.info(f"Found matching pipeline: {pipeline}")
            if not self.pipeline:
                raise Exception("no CI_PIPELINE_ID given ( export CI_PIPELINE_ID")

        logger.info(f"Using pipeline_id: {self.pipeline.id}")
        self.target_dir = "signing_dir"
        Path(self.target_dir).mkdir(parents=True, exist_ok=True)

    def download_and_unpack_all_artifacts(self):
        if os.path.isdir(self.target_dir):
            logger.info(f"First purging {self.target_dir}")
            shutil.rmtree(self.target_dir)
        for job in self.pipeline.jobs.list():
            if job.name in [
                "release_electron_linux_windows",
                "release_binary_windows",
                "release_pip",
            ]:
                zipfn = f"/tmp/_artifacts_{job.name}.zip"
                job_obj = self.project.jobs.get(job.id, lazy=True)

                if not os.path.isfile(zipfn):
                    logger.info(f"Downloading artifacts for {job.name}")
                    with open(zipfn, "wb") as f:
                        job_obj.artifacts(streamed=True, action=f.write)
                else:
                    logger.info(f"Skipping Download artifacts for {job.name}")

                logger.info(f"Unzipping {zipfn} in target-folder")
                with zipfile.ZipFile(zipfn, "r") as zip:
                    for zip_info in zip.infolist():
                        if zip_info.filename[-1] == "/":
                            continue
                        zip_info.filename = os.path.basename(zip_info.filename)
                        logger.info(f" Extracting {zip_info.filename}")
                        zip.extract(zip_info, self.target_dir)

    def download_and_unpack_new_artifacts_from_github(self):
        from utils import github

        gc = github.GithubConnection(self.github_project)
        release = gc.fetch_existing_release(self.tag)
        assets = gc.list_assets(release)
        for asset in assets:
            if not asset.name.startswith("SHA256"):
                continue
            if asset.name == "SHA256SUMS" or asset.name == "SHA256SUMS.asc":
                continue
            if asset.name.endswith(".asc"):
                continue
            shasumfile = Sha256sumFile(asset.name)
            if not shasumfile.is_in_target_dir():
                shasumfile.download_from_tag(self.tag, gc)
                shasumfile.download_hashed_files(gc)
                shasumfile.check_hashes()
                shasumfile.check_sig()

    def create_sha256sum_file(self):
        with open(f"{self.target_dir}/SHA256SUMS", "w") as shafile:
            for file in os.listdir(self.target_dir):
                if file.startswith("SHA256SUMS-") and not file.endswith(".asc"):
                    logger.debug(f"Processing {file}")
                    sha_src = Sha256sumFile(file)
                    sha_src.read()
                    for hashed_file in sha_src.hashed_files.keys():
                        print(f"{sha_src.hashed_files[hashed_file]} {hashed_file}\n")
                        shafile.write(
                            f"{sha_src.hashed_files[hashed_file]} {hashed_file}\n"
                        )
        returncode = subprocess.call(
            ["sha256sum", "-c", "SHA256SUMS"], cwd=self.target_dir
        )
        if returncode != 0:
            raise Exception(
                f"One of the hashes is not matching: {subprocess.run(['sha256sum', '-c', 'SHA256SUMS'], cwd=self.target_dir)}"
            )

    def check_all_hashes(self):
        for file in os.listdir(self.target_dir):
            if file.startswith("SHA256SUM") and not file.endswith(".asc"):
                returncode = subprocess.call(
                    ["sha256sum", "-c", file], cwd=self.target_dir
                )
                if returncode != 0:
                    raise Exception(f"Could not validate hashes for file {file}")

    def check_all_sigs(self):
        for file in os.listdir(self.target_dir):
            if file.endswith(".asc"):
                returncode = subprocess.call(
                    ["gpg", "--verify", file], cwd=self.target_dir
                )
                if returncode != 0:
                    raise Exception(
                        f"Could not validate signature of file {file}: {subprocess.run(['gpg', '--verify', file], cwd=self.target_dir)}"
                    )

    def calculate_publish_params(self):
        if not "CI_PROJECT_ROOT_NAMESPACE" in os.environ:
            logger.error("CI_PROJECT_ROOT_NAMESPACE not found")
            exit(2)
        else:
            self.github_project = (
                f"{os.environ['CI_PROJECT_ROOT_NAMESPACE']}/specter-desktop"
            )
        if not "CI_COMMIT_TAG" in os.environ:
            logger.error("CI_COMMIT_TAG not found")
            exit(2)
        else:
            tag = os.environ["CI_COMMIT_TAG"]
        if not "GH_BIN_UPLOAD_PW" in os.environ:
            logger.error("GH_BIN_UPLOAD_PW not found.")
            exit(2)
        else:
            self.password = os.environ["GH_BIN_UPLOAD_PW"]

    def upload_sha256sum_file(self):
        artifact = os.path.join("signing_dir", "SHA256SUMS")
        self.calculate_publish_params()

        if self.github.artifact_exists(
            self.github_project, self.tag, Path(artifact).name
        ):
            logger.info(f"Github artifact {artifact} existing. Skipping upload.")
            exit(0)
        else:
            logger.info(f"Github artifact {artifact} does not exist. Let's upload!")
        self.github.publish_release_from_tag(
            self.github_project,
            self.tag,
            [artifact],
            "github.com",
            "gitlab_upload_release_binaries",
            self.password,
        )


def sha256sum(filenames):
    sha_file = Sha256sumFile("SHA256SUMS", target_dir=".")
    for filename in filenames:
        logger.info(f"Adding {filename}")
        sha_file.add_file(filename)
    sha_file.print()


if __name__ == "__main__":
    if "sha256sums" in sys.argv:
        sha256sum(sys.argv[2:])
        exit(0)
    rh = ReleaseHelper()
    rh.init_gitlab()
    if "download" in sys.argv:
        rh.download_and_unpack_all_artifacts()
    if "downloadgithub" in sys.argv:
        rh.download_and_unpack_new_artifacts_from_github()
    if "checkhashes" in sys.argv:
        rh.check_all_hashes()
    if "checksigs" in sys.argv:
        rh.check_all_sigs()
    if "create" in sys.argv:
        rh.create_sha256sum_file()
    if "upload" in sys.argv:
        rh.upload_sha256sum_file()
