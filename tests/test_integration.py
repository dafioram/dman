"""Integration tests against a real Docker daemon and Compose plugin.

They create and then remove a throwaway Compose project (containers, a named volume,
an anonymous volume and a locally built image), so they only run when asked:

    DMAN_INTEGRATION=1 python3 -m unittest discover -s tests -v

The image is built FROM scratch and the containers are created but never started,
so no registry access is needed.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dman  # noqa: E402

ENABLED = os.environ.get("DMAN_INTEGRATION") == "1"

COMPOSE = """\
services:
  app:
    build: .
    image: {name}-img:test
    volumes:
      - data:/data
volumes:
  data: {{}}
"""

DOCKERFILE = """\
FROM scratch
COPY marker /marker
VOLUME /anon
CMD ["/nothing"]
"""


@unittest.skipUnless(ENABLED, "set DMAN_INTEGRATION=1 to run against the local Docker daemon")
class ComposeProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        problem = dman.preflight()
        if problem:
            raise unittest.SkipTest("Docker is not usable here: " + problem)
        cls.tmp = tempfile.mkdtemp()
        cls.name = "dman-it-" + uuid.uuid4().hex[:8]
        cls.folder = os.path.join(cls.tmp, cls.name)
        os.mkdir(cls.folder)
        for fname, text in {"compose.yaml": COMPOSE.format(name=cls.name),
                            "Dockerfile": DOCKERFILE, "marker": "x\n"}.items():
            with open(os.path.join(cls.folder, fname), "w") as fh:
                fh.write(text)
        cls.cfg = dman.Config(None, [cls.tmp], 1, None)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "compose", "-p", cls.name, "--project-directory", cls.folder,
                        "down", "-v"], capture_output=True)
        subprocess.run(["docker", "image", "rm", "-f", cls.name + "-img:test"], capture_output=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def project(self):
        projects, _ = dman.build_projects(dman.list_containers()[0], self.cfg)
        return next(p for p in projects if p.directory and os.path.basename(p.directory) == self.name)

    def test_lifecycle(self):
        p = self.project()
        self.assertEqual((p.name, p.status, p.name_known, p.files_how), (self.name, "not deployed", False, "default"))

        r = dman.run(dman.compose_base(p) + ["up", "--no-start"], cwd=dman.compose_cwd(p), timeout=300)
        self.assertTrue(r.ok, r.message())

        p = self.project()
        self.assertTrue(p.name_known)
        self.assertEqual(p.files_how, "deployed")
        self.assertEqual(p.files, [os.path.join(os.path.realpath(self.folder), "compose.yaml")])
        self.assertEqual(p.status, "stopped")
        self.assertEqual(list(p.services()), ["app"])
        ct = p.containers[0]
        self.assertEqual((ct.project, ct.service, ct.state), (self.name, "app", "created"))

        impact, _ = dman.current_impact(p)
        self.assertTrue(impact.certain, impact.notes)
        self.assertEqual(impact.remove, [self.name + "_data"])
        self.assertEqual(len(impact.anonymous), 1)

        rows = {r.name: r for r in dman.build_mount_rows(dman.list_volumes()[0], dman.list_containers()[0])}
        named = rows[self.name + "_data"]
        self.assertEqual((named.vtype, named.projects), ("named", [self.name]))
        self.assertEqual([u.container.id for u in named.users], [ct.id])
        self.assertEqual(rows[impact.anonymous[0]].vtype, "anonymous")

        images, err = dman.list_images()
        self.assertIsNone(err)
        img = next(i for i in images if self.name + "-img:test" in i.tags)
        self.assertEqual([u.id for u in dman.image_users(img, p.containers)], [ct.id])

        r = dman.run(dman.compose_base(p) + ["down", "-v"], cwd=dman.compose_cwd(p), timeout=120)
        self.assertTrue(r.ok, r.message())
        p = self.project()
        self.assertEqual(p.status, "not deployed")
        self.assertNotIn(self.name + "_data", dman.docker("volume", "ls", "-q").out.split())


if __name__ == "__main__":
    unittest.main()
