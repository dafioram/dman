"""Unit tests for dman's parsing and safety logic. No Docker daemon needed.

Run from the package folder:  python3 -m unittest discover -s tests -v
"""
import configparser
import io
import os
import sys
import tempfile
import unittest
from datetime import timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dman  # noqa: E402


def make_container(name, state="running", labels=None, mounts=None, image_id="sha256:aa"):
    return dman.container_from_inspect({
        "Id": (name.encode().hex() * 8)[:64], "Name": "/" + name, "Image": image_id,
        "Created": "2026-01-01T00:00:00.123456789Z",
        "State": {"Status": state, "ExitCode": 0 if state == "running" else 1,
                  "StartedAt": "2026-01-01T00:00:01Z", "FinishedAt": "0001-01-01T00:00:00Z"},
        "Config": {"Image": "img:1", "Labels": labels or {}, "Env": ["A=1"]},
        "NetworkSettings": {"Ports": {}, "Networks": {"bridge": {}}},
        "HostConfig": {"RestartPolicy": {"Name": "no"}},
        "Mounts": mounts or [],
    })


def cfg_with(roots, depth=2):
    cp = configparser.ConfigParser()
    cp.read_string("[projects]\nroots =\n" + "".join("  %s\n" % r for r in roots) + "depth = %d\n" % depth)
    return dman.parse_config(cp, None, [])


class VersionTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(dman.parse_version("1"), 1)
        self.assertEqual(dman.parse_version("12\n"), 12)

    def test_invalid(self):
        for bad in ["", "0", "-1", "1.0", "v1", "1\n\n", " 1", "1 ", "abc", "1\n2"]:
            self.assertIsNone(dman.parse_version(bad), bad)


class ParsingTests(unittest.TestCase):
    def test_timestamps(self):
        dt = dman.parse_ts("2026-10-19T03:02:57.005958612Z")
        self.assertEqual((dt.year, dt.second, dt.microsecond), (2026, 57, 5958))
        self.assertEqual(dt.utcoffset().total_seconds(), 0)
        self.assertEqual(dman.parse_ts("2026-10-18T23:02:56-04:00").utcoffset().total_seconds(), -4 * 3600)
        self.assertIsNone(dman.parse_ts("0001-01-01T00:00:00Z"))
        self.assertIsNone(dman.parse_ts("garbage"))

    def test_ports(self):
        ports = {
            "90/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9090"}],
            "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}, {"HostIp": "::", "HostPort": "8080"}],
            "53/udp": [{"HostIp": "", "HostPort": "53"}],
            "443/tcp": None,
        }
        self.assertEqual(dman.format_ports(ports), ["53->53/udp", "8080->80", "127.0.0.1:9090->90"])
        self.assertEqual(dman.format_ports({}), [])
        self.assertEqual(dman.format_ports(None), [])

    def test_json_helpers(self):
        self.assertEqual(dman.parse_json_array('[{"a":1}]'), [{"a": 1}])
        self.assertEqual(dman.parse_json_array("Error: no such object"), [])
        self.assertEqual(dman.parse_json_array("[not json"), [])
        self.assertEqual(dman.parse_json_lines('{"a":1}\nnoise\n{"b":2}\n'), [{"a": 1}, {"b": 2}])

    def test_selection(self):
        self.assertEqual(dman.parse_selection("1 3-4,2", 5), [0, 1, 2, 3])
        self.assertEqual(dman.parse_selection("all", 3), [0, 1, 2])
        self.assertEqual(dman.parse_selection("4-2", 5), [1, 2, 3])
        for bad in ["", "0", "6", "1-9", "x", "1 two", "\u00b2", "1 \u00b3"]:
            self.assertIsNone(dman.parse_selection(bad, 5), bad)

    def test_dotenv(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            with open(p, "w") as fh:
                fh.write("# c\nA=1\nexport B='two words'\nC=\"x\"\nnot a pair\n\n")
            pairs, err = dman.read_dotenv(p)
            self.assertIsNone(err)
            self.assertEqual(pairs, [("A", "1"), ("B", "two words"), ("C", "x")])
            self.assertEqual(dman.read_dotenv(os.path.join(d, "missing")), (None, None))

    def test_redaction_default(self):
        lines = dman.redacted_lines([("PASSWORD", "hunter2"), ("EMPTY", "")], reveal=False)
        self.assertEqual(lines, ["PASSWORD=<hidden>", "EMPTY=<empty>"])
        self.assertNotIn("hunter2", "".join(lines))
        self.assertEqual(dman.redacted_lines([("K", "v")], reveal=True), ["K=v"])

    def test_volume_classification(self):
        hexname = "a" * 64
        self.assertEqual(dman.classify_volume("data", None), "named")
        self.assertEqual(dman.classify_volume(hexname, {dman.L_ANON: ""}), "anonymous")
        self.assertEqual(dman.classify_volume(hexname, {}), "unknown")
        self.assertEqual(dman.classify_volume(hexname, {dman.L_CVOLUME: "x"}), "named")


class ConfigTests(unittest.TestCase):
    def test_parse(self):
        cp = configparser.ConfigParser()
        cp.read_string("[projects]\nroots =\n  /a\n  # comment\n  /b\n  /a\ndepth = 9\n[ui]\npage_size = 15\n")
        cfg = dman.parse_config(cp, "x", [])
        self.assertEqual(cfg.roots, ["/a", "/b"])
        self.assertEqual(cfg.depth, dman.MAX_DEPTH)
        self.assertEqual(cfg.page_size, 15)

    def test_bad_values_warn(self):
        cp = configparser.ConfigParser()
        cp.read_string("[projects]\ndepth = two\n[ui]\npage_size = big\n")
        warnings = []
        cfg = dman.parse_config(cp, "x", warnings)
        self.assertEqual(cfg.depth, dman.DEFAULT_DEPTH)
        self.assertEqual(len(warnings), 2)

    def test_non_ascii_digits_warn(self):
        cp = configparser.ConfigParser()
        cp.read_string("[projects]\ndepth = \u00b2\n[ui]\npage_size = \u00b9\n")
        warnings = []
        cfg = dman.parse_config(cp, "x", warnings)
        self.assertEqual((cfg.depth, cfg.page_size, len(warnings)), (dman.DEFAULT_DEPTH, None, 2))

    def test_missing_explicit_file(self):
        cfg = dman.load_config("/nonexistent/dman.conf")
        self.assertIsNone(cfg.path)
        self.assertTrue(cfg.warnings)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        files = {
            "web/compose.yaml": "", "web/compose.override.yaml": "", "web/notes.yaml": "",
            "group/api/docker-compose.yml": "",
            "group/variant/docker-compose.prod.yml": "",
            "multi/docker-compose.dev.yml": "", "multi/docker-compose.prod.yml": "",
            "a/b/c/compose.yaml": "",            # depth 3: not found at depth 2
            ".hidden/compose.yaml": "",          # hidden: skipped
            "node_modules/x/compose.yaml": "",   # skipped
        }
        for rel, text in files.items():
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(text)
        os.symlink(self.root, os.path.join(self.root, "loop"))

    def tearDown(self):
        self.tmp.cleanup()

    def rel(self, folders):
        return sorted(os.path.relpath(f.path, self.root) for f in folders)

    def test_depth_and_skips(self):
        folders, notes = dman.scan_folders([self.root, "/nonexistent-root"], 2)
        self.assertEqual(self.rel(folders), ["group/api", "group/variant", "multi", "web"])
        self.assertTrue(any("nonexistent-root" in n for n in notes))
        web = next(f for f in folders if f.path.endswith("web"))
        self.assertEqual(web.files, ["compose.override.yaml", "compose.yaml"])

    def test_depth_limits(self):
        self.assertEqual(self.rel(dman.scan_folders([self.root], 1)[0]), ["multi", "web"])
        self.assertIn("a/b/c", self.rel(dman.scan_folders([self.root], 3)[0]))

    def test_default_file_sets(self):
        self.assertEqual(dman.default_file_set(["compose.yaml", "compose.override.yaml"]),
                         ["compose.yaml", "compose.override.yaml"])
        self.assertEqual(dman.default_file_set(["docker-compose.yml", "docker-compose.override.yml",
                                                "docker-compose.prod.yml"]),
                         ["docker-compose.yml", "docker-compose.override.yml"])
        self.assertEqual(dman.default_file_set(["compose.yml", "compose.yaml"]), ["compose.yaml"])
        self.assertEqual(dman.default_file_set(["docker-compose.prod.yml"]), ["docker-compose.prod.yml"])
        self.assertIsNone(dman.default_file_set(["docker-compose.dev.yml", "docker-compose.prod.yml"]))

    def test_projects_merge_folders_and_labels(self):
        web_dir = os.path.join(self.root, "web")
        labels = {dman.L_PROJECT: "webby", dman.L_SERVICE: "app", dman.L_WORKDIR: web_dir,
                  dman.L_FILES: os.path.join(web_dir, "compose.yaml")}
        ghost = {dman.L_PROJECT: "ghost", dman.L_SERVICE: "g", dman.L_WORKDIR: "/gone",
                 dman.L_FILES: "/gone/compose.yaml"}
        cts = [make_container("webby-app-1", labels=labels),
               make_container("ghost-g-1", state="exited", labels=ghost),
               make_container("plain")]
        projects, _ = dman.build_projects(cts, cfg_with([self.root]))
        by = {p.name: p for p in projects}
        self.assertEqual(sorted(by), ["api", "ghost", "multi", "variant", "webby"])
        webby = by["webby"]
        self.assertTrue(webby.name_known)
        self.assertEqual(webby.files, [os.path.join(web_dir, "compose.yaml")])
        self.assertEqual(webby.files_how, "deployed")
        self.assertEqual(webby.status, "running")
        self.assertFalse(by["ghost"].files)
        self.assertEqual(by["ghost"].status_text, "stopped (no file)")
        self.assertTrue(by["ghost"].problems)
        self.assertEqual(by["api"].status, "not deployed")
        self.assertFalse(by["api"].name_known)
        self.assertEqual(by["multi"].status, "pick files")
        self.assertFalse(by["multi"].can_up)

    def test_compose_base(self):
        web_dir = os.path.join(self.root, "web")
        folder_only = dman.Project(key="folder:x", name="web", name_known=False, directory=web_dir,
                                   folder_files=[], files=[web_dir + "/compose.yaml"], files_how="default",
                                   containers=[])
        self.assertEqual(dman.compose_base(folder_only),
                         ["docker", "compose", "--project-directory", web_dir, "-f", web_dir + "/compose.yaml"])
        labelled = dman.Project(key="project:p", name="p", name_known=True, directory="/gone",
                                folder_files=[], files=None, files_how="", containers=[])
        self.assertEqual(dman.compose_base(labelled), ["docker", "compose", "-p", "p"])


class DownVolumesImpactTests(unittest.TestCase):
    def test_impact(self):
        anon = "b" * 64
        ct = make_container("p-a-1", labels={dman.L_PROJECT: "p"},
                            mounts=[{"Type": "volume", "Name": anon, "Destination": "/x"},
                                    {"Type": "volume", "Name": "p_data", "Destination": "/d"}])
        p = dman.Project(key="project:p", name="p", name_known=True, directory=None, folder_files=[],
                         files=["/x/compose.yaml"], files_how="deployed", containers=[ct])
        cfg = {"volumes": {"data": {"name": "p_data"}, "cache": {}, "shared": {"name": "ext", "external": True}}}
        impact = dman.volume_impact(p, cfg, None, ["p_data", "ext", "other"])
        self.assertEqual(impact.remove, ["p_data"])
        self.assertEqual(impact.absent, ["p_cache"])
        self.assertEqual(impact.external, ["ext"])
        self.assertEqual(impact.anonymous, [anon])
        self.assertTrue(impact.certain)
        unknown = dman.volume_impact(p, None, "boom", ["p_data"])
        self.assertFalse(unknown.certain)
        self.assertTrue(unknown.notes)
        self.assertNotEqual(impact.snapshot(), dman.volume_impact(p, cfg, None, []).snapshot())


class EndpointTests(unittest.TestCase):
    def runner(self, context="default", host="unix:///var/run/docker.sock", ok=True):
        def fake(*args, timeout=0):
            if args[:2] == ("context", "show"):
                return dman.Result(0 if ok else 1, context + "\n", "" if ok else "boom")
            if args[:2] == ("context", "inspect"):
                return dman.Result(0, '"%s"\n' % host, "")
            return dman.Result(1, "", "unexpected")
        return fake

    def check(self, env, runner=None, socket=True):
        return dman.resolve_endpoint(env, runner or self.runner(), lambda p: socket)

    def test_local_default(self):
        ep = self.check({})
        self.assertIsNone(ep.problem)
        self.assertEqual(ep.host, "unix:///var/run/docker.sock")

    def test_remote_contexts_refused(self):
        for host in ["tcp://10.0.0.2:2375", "ssh://u@h", "npipe:////./pipe/docker_engine"]:
            self.assertIsNotNone(self.check({}, self.runner("remote", host)).problem, host)

    def test_docker_host_override(self):
        self.assertIsNone(self.check({"DOCKER_HOST": "unix:///run/docker.sock"}).problem)
        self.assertIsNotNone(self.check({"DOCKER_HOST": "tcp://1.2.3.4:2376"}).problem)

    def test_ambiguous(self):
        ep = self.check({"DOCKER_HOST": "unix:///run/docker.sock", "DOCKER_CONTEXT": "other"})
        self.assertIn("ambiguous", ep.problem)

    def test_missing_socket_and_errors(self):
        self.assertIsNotNone(self.check({}, socket=False).problem)
        self.assertIsNotNone(self.check({}, self.runner(ok=False)).problem)
        self.assertIsNotNone(self.check({}, self.runner(host="")).problem)


class ProjectWorkflowTests(unittest.TestCase):
    def test_service_names_without_files(self):
        cts = [make_container("p-web-1", labels={dman.L_PROJECT: "p", dman.L_SERVICE: "web"}),
               make_container("p-db-1", labels={dman.L_PROJECT: "p", dman.L_SERVICE: "db"})]
        p = dman.Project(key="project:p", name="p", name_known=True, directory=None, folder_files=[],
                         files=None, files_how="", containers=cts)
        self.assertEqual(dman.service_names(p), (["db", "web"], None))


class LogTests(unittest.TestCase):
    def test_options(self):
        self.assertEqual(dman.log_options("100"), ["--tail", "100"])
        self.assertEqual(dman.log_options("100", follow=True), ["-f", "--tail", "100"])
        self.assertEqual(dman.log_options("100", follow=True, timestamps=True), ["-f", "-t", "--tail", "100"])
        self.assertEqual(dman.log_options(since="15m"), ["--since", "15m"])
        self.assertEqual(dman.log_options(), [])

    def test_since_values(self):
        for ok in ["15m", "2h30m", "1.5h", "90s", "2026-10-22", "2026-10-22T10:00",
                   "2026-10-22T10:00:00Z", "2026-10-22T10:00:00+02:00", "1761100000"]:
            self.assertTrue(dman.LOG_SINCE_RE.fullmatch(ok), ok)
        for bad in ["", "15", "m", "yesterday", "15 m", "--tail", "2026-10"]:
            self.assertFalse(dman.LOG_SINCE_RE.fullmatch(bad), bad)


class LayoutTests(unittest.TestCase):
    def test_columns_fit_and_drop(self):
        cols = [dman.Col("NAME", str, max=30, min=6, flex=True, want=12),
                dman.Col("PORTS", str, max=30, min=6, flex=True, want=10),
                dman.Col("AGE", str, max=8, drop=1)]
        cells = [["a-very-long-container-name-01", "8080->80, 9090->90, 1->1", "up 3d"]]
        wide = dman.layout_columns(cols, cells, 200)
        self.assertEqual([i for i, _ in wide], [0, 1, 2])
        narrow = dman.layout_columns(cols, cells, 30)
        self.assertEqual([i for i, _ in narrow], [0, 1])
        self.assertLessEqual(sum(w for _, w in narrow) + 2 * (len(narrow) - 1), 30)

    def test_fit(self):
        dman.Style.ellipsis = "~"
        self.assertEqual(dman.fit("abcdef", 4), "abc~")
        self.assertEqual(dman.fit("abc", 4), "abc")
        self.assertEqual(dman.fit("abc", 0), "")


class OutputSafetyTests(unittest.TestCase):
    def test_strip_controls(self):
        hostile = ("a\x1b]52;c;aGk=\x07b"      # OSC 52 clipboard write
                   "\x1b]0;title\x1b\\c"        # window title
                   "\x1b[5A\x1b[2Jd\x1b[8m"      # cursor up, clear, conceal
                   "\x9b31m\re\x00f")           # C1 CSI, carriage return, NUL
        out = dman.strip_controls(hostile)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x9b", out)
        self.assertNotIn("\r", out)
        self.assertNotIn("\x07", out)
        self.assertEqual(dman.strip_controls("x\ty\nz \u2026 \u00e9"), "x\ty\nz \u2026 \u00e9")

    def test_own_styles_pass(self):
        dman.Style.enabled = True
        try:
            styled = dman.c("ok", "bold", "red") + dman.c("x", "dim")
        finally:
            dman.Style.enabled = False
        self.assertEqual(dman.strip_controls(styled), styled)

    def test_safe_output(self):
        buf = io.StringIO()
        out = dman.SafeOutput(buf)
        print("name\x1b]0;pwned\x07!", file=out)
        self.assertEqual(buf.getvalue(), "name]0;pwned!\n")
        self.assertEqual(out.getvalue(), buf.getvalue())


class ContainerTests(unittest.TestCase):
    def test_from_inspect(self):
        ct = make_container("x", state="exited", labels={dman.L_PROJECT: "p", dman.L_SERVICE: "s"})
        self.assertEqual(ct.name, "x")
        self.assertEqual(ct.state_text(), "exited (1)")
        self.assertTrue(ct.stopped)
        self.assertEqual((ct.project, ct.service), ("p", "s"))
        self.assertEqual(ct.created.tzinfo, timezone.utc)

    def test_restart_count(self):
        ct = make_container("loop")
        self.assertEqual((ct.restart_count, ct.state_text(), dman.state_style(ct)), (0, "running", ("green",)))
        ct.restart_count = 1
        self.assertEqual(ct.state_text(), "running (1 restart)")
        ct.restart_count = 7
        self.assertEqual(ct.state_text(), "running (7 restarts)")
        self.assertEqual(dman.state_style(ct), ("yellow",))
        self.assertIn("restarted 7 times", dman.container_details(ct))
        raw = dict(make_container("x").raw, RestartCount=3)
        self.assertEqual(dman.container_from_inspect(raw).restart_count, 3)

    def test_mount_rows(self):
        vol = dman.Volume("data", "local", "/var/lib/docker/volumes/data/_data", None, {dman.L_PROJECT: "p"})
        unused = dman.Volume("spare", "local", "/v/spare", None, {})
        ct = make_container("c", mounts=[{"Type": "volume", "Name": "data", "Destination": "/d", "RW": True},
                                         {"Type": "bind", "Source": "/srv/x", "Destination": "/x", "RW": False}])
        rows = {r.name: r for r in dman.build_mount_rows([vol, unused], [ct])}
        self.assertEqual([u.container.name for u in rows["data"].users], ["c"])
        self.assertEqual(rows["data"].projects, ["p"])
        self.assertEqual(rows["spare"].users, [])
        self.assertEqual(rows["/srv/x"].kind, "bind")
        self.assertFalse(rows["/srv/x"].users[0].rw)


if __name__ == "__main__":
    unittest.main()
