import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import agent


class DockerStatusTests(unittest.TestCase):

    @mock.patch("agent.run_command")
    def test_returns_each_container_with_display_state(self, run_command):
        run_command.return_value = (
            0,
            "\n".join([
                "web|example/web:latest|running|Up 2 hours",
                "db|postgres:16|exited|Exited (0) 1 hour ago",
                "worker|example/worker:latest|running|Up 1 minute (unhealthy)",
            ]),
            ""
        )

        result = agent.docker_status()

        self.assertTrue(result["available"])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["running"], 2)
        self.assertEqual(
            [(item["name"], item["state"]) for item in result["containers"]],
            [
                ("worker", "warning"),
                ("db", "down"),
                ("web", "up"),
            ]
        )

    @mock.patch("agent.run_command")
    def test_returns_empty_list_when_docker_is_unavailable(self, run_command):
        run_command.return_value = (1, "", "permission denied")

        result = agent.docker_status()

        self.assertFalse(result["available"])
        self.assertEqual(result["containers"], [])


class AuthorizationTests(unittest.TestCase):

    def test_bearer_token_must_match(self):
        handler = object.__new__(agent.AgentHandler)
        handler.agent_token = "expected-token"
        handler.headers = {
            "Authorization": "Bearer expected-token"
        }

        self.assertTrue(handler.is_authorized())

        handler.headers = {
            "Authorization": "Bearer wrong-token"
        }

        self.assertFalse(handler.is_authorized())


class StatusContractTests(unittest.TestCase):

    @mock.patch("agent.snapraid_status")
    @mock.patch("agent.docker_status")
    @mock.patch("agent.storage_status")
    @mock.patch("agent.wan_available", return_value=True)
    @mock.patch("agent.ping", return_value=True)
    @mock.patch("agent.default_gateway", return_value="10.0.0.1")
    @mock.patch("agent.load_average", return_value=0.5)
    @mock.patch("agent.uptime_seconds", return_value=3600)
    @mock.patch("agent.temperature", return_value=42.0)
    @mock.patch("agent.memory_status")
    @mock.patch("agent.cpu_percent", return_value=12.5)
    @mock.patch("agent.lan_ip", return_value="10.0.0.10")
    @mock.patch("agent.hostname", return_value="test-host")
    def test_status_advertises_versioned_contract_and_capabilities(
            self,
            hostname,
            lan_ip,
            cpu_percent,
            memory_status,
            temperature,
            uptime_seconds,
            load_average,
            default_gateway,
            ping,
            wan_available,
            storage_status,
            docker_status,
            snapraid_status):

        memory_status.return_value = {
            "percent": 25.0,
            "used_mb": 256,
            "total_mb": 1024
        }
        storage_status.return_value = []
        docker_status.return_value = {
            "available": True,
            "total": 0,
            "running": 0,
            "containers": [],
            "services": []
        }
        snapraid_status.return_value = {
            "available": False,
            "state": "missing",
            "summary": "SnapRAID not found"
        }

        result = agent.build_status()

        self.assertEqual(
            result["schema"],
            {
                "name": "dashboard-agent-status",
                "version": 1
            }
        )
        self.assertEqual(result["agent"]["name"], "dashboard-agent")
        self.assertEqual(result["agent"]["version"], agent.AGENT_VERSION)
        self.assertIn("status.v1", result["agent"]["capabilities"])
        self.assertIn("metrics.docker", result["agent"]["capabilities"])
        self.assertEqual(result["host"]["hostname"], "test-host")


class DockerControlTests(unittest.TestCase):

    @mock.patch("agent.run_command")
    def test_executes_allowed_action(self, run_command):
        run_command.return_value = (0, "plex", "")
        status, response = agent.execute_container_action("plex", "restart")
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(response["action"], "restart")

    def test_rejects_invalid_action(self):
        status, response = agent.execute_container_action("plex", "exec")
        self.assertEqual(status, 400)
        self.assertIn("error", response)

    def test_rejects_invalid_container_name(self):
        status, response = agent.execute_container_action("plex; rm -rf /", "stop")
        self.assertEqual(status, 403)

    @mock.patch("agent.run_command")
    def test_fetches_container_logs(self, run_command):
        run_command.return_value = (0, "line 1\nline 2", "")
        status, response = agent.get_container_logs("plex", lines=50)
        self.assertEqual(status, 200)
        self.assertEqual(response["logs"], "line 1\nline 2")


class HostPowerControlTests(unittest.TestCase):

    def test_power_action_rejected_when_not_enabled(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            status, response = agent.execute_host_power_action("reboot")
            self.assertEqual(status, 403)
            self.assertIn("disabled", response["error"])

    @mock.patch("agent.run_command")
    def test_power_action_executes_when_enabled(self, run_command):
        run_command.return_value = (0, "rebooting", "")
        with mock.patch.dict("os.environ", {"DASHBOARD_AGENT_ALLOW_POWER": "1"}):
            status, response = agent.execute_host_power_action("reboot")
            self.assertEqual(status, 200)
            self.assertTrue(response["ok"])


class EnrollmentProtocolTests(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = self.temp_dir.name
        agent.AgentHandler.data_dir = self.data_dir
        agent.AgentHandler.agent_token = ""

    def tearDown(self):
        self.temp_dir.cleanup()
        agent.AgentHandler.agent_token = ""
        agent.AgentHandler.data_dir = agent.DATA_DIR

    def test_crockford_base32_and_verification_code(self):
        code = agent.compute_verification_code(
            bootstrap_token="boot-token-12345",
            enrollment_id="enroll-uuid-111",
            agent_id="agent-uuid-222"
        )
        self.assertEqual(len(code), 9)
        self.assertEqual(code[4], "-")
        raw_chars = code.replace("-", "")
        self.assertEqual(len(raw_chars), 8)
        for char in raw_chars:
            self.assertIn(char, agent.CROCKFORD_ALPHABET)

        # Deterministic check
        code2 = agent.compute_verification_code(
            bootstrap_token="boot-token-12345",
            enrollment_id="enroll-uuid-111",
            agent_id="agent-uuid-222"
        )
        self.assertEqual(code, code2)

    def test_get_or_create_agent_id_persists(self):
        agent_id1 = agent.get_or_create_agent_id(self.data_dir)
        self.assertTrue(len(agent_id1) >= 32)
        agent_id2 = agent.get_or_create_agent_id(self.data_dir)
        self.assertEqual(agent_id1, agent_id2)

    def test_enrollment_probe_and_expiration(self):
        enrollment_file = agent.Path(self.data_dir) / "enrollment.json"
        agent.atomic_write_json(enrollment_file, {
            "enrollment_id": "test-enroll-id",
            "bootstrap_token": "test-boot-token",
            "expires_at": int(time.time()) + 1800
        })

        handler = object.__new__(agent.AgentHandler)
        handler.data_dir = self.data_dir
        handler.headers = {"Authorization": "Bearer test-boot-token"}

        ok, code, payload = handler.check_enrollment_auth()
        self.assertTrue(ok)
        self.assertEqual(code, 200)
        self.assertEqual(payload["enrollment_id"], "test-enroll-id")

        # Wrong token
        handler.headers = {"Authorization": "Bearer wrong-token"}
        ok, code, payload = handler.check_enrollment_auth()
        self.assertFalse(ok)
        self.assertEqual(code, 401)

        # Expired enrollment
        agent.atomic_write_json(enrollment_file, {
            "enrollment_id": "test-enroll-id",
            "bootstrap_token": "test-boot-token",
            "expires_at": int(time.time()) - 10
        })
        handler.headers = {"Authorization": "Bearer test-boot-token"}
        ok, code, payload = handler.check_enrollment_auth()
        self.assertFalse(ok)
        self.assertEqual(code, 410)
        self.assertFalse(enrollment_file.exists())

    def test_commit_rotates_credentials_and_is_idempotent(self):
        import io
        enrollment_file = agent.Path(self.data_dir) / "enrollment.json"
        agent.atomic_write_json(enrollment_file, {
            "enrollment_id": "test-enroll-id",
            "bootstrap_token": "test-boot-token",
            "expires_at": int(time.time()) + 1800
        })

        # Commit permanent token
        handler = object.__new__(agent.AgentHandler)
        handler.data_dir = self.data_dir
        body_bytes = b'{"permanent_token":"permanent-secret-1"}'
        handler.headers = {
            "Authorization": "Bearer test-boot-token",
            "Content-Length": str(len(body_bytes))
        }
        handler.rfile = io.BytesIO(body_bytes)
        sent = []
        handler.send_json = lambda status, body: sent.append((status, body))

        handler.path = "/api/enroll/commit"
        handler.do_POST()

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 200)
        self.assertEqual(sent[0][1]["status"], "committed")

        # Enrollment file should be removed
        self.assertFalse(enrollment_file.exists())

        # Credentials file should exist with permanent token
        creds_file = agent.Path(self.data_dir) / "credentials.json"
        self.assertTrue(creds_file.exists())
        creds_data = json.loads(creds_file.read_text(encoding="utf-8"))
        self.assertEqual(creds_data["token"], "permanent-secret-1")

        # Re-commit is idempotent
        sent.clear()
        handler.headers = {
            "Authorization": "Bearer permanent-secret-1",
            "Content-Length": str(len(body_bytes))
        }
        handler.rfile = io.BytesIO(body_bytes)
        handler.do_POST()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 200)
        self.assertTrue(sent[0][1].get("idempotent"))

        # Permanent token authorizes requests
        handler.headers = {"Authorization": "Bearer permanent-secret-1"}
        self.assertTrue(handler.is_authorized())

        # Old bootstrap token rejected
        handler.headers = {"Authorization": "Bearer test-boot-token"}
        self.assertFalse(handler.is_authorized())


class AgentSelfUpdateTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lib_dir = Path(self.temp_dir.name) / "lib"
        self.lib_dir.mkdir()
        self.agent_file = self.lib_dir / "agent.py"
        self.agent_file.write_text('#!/usr/bin/env python3\nAGENT_VERSION = "0.3.0"\n', encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_update_requires_authorization(self):
        handler = object.__new__(agent.AgentHandler)
        handler.agent_token = "perm-token"
        handler.headers = {"Authorization": "Bearer wrong-token"}
        handler.path = "/api/update"
        sent = []
        handler.send_json = lambda status, body: sent.append((status, body))

        handler.do_POST()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 401)

    @mock.patch("urllib.request.urlopen")
    def test_update_downloads_validates_and_replaces(self, mock_urlopen):
        new_code = (
            b'#!/usr/bin/env python3\n'
            b'# Kindle Dashboard Agent Self-Update Test File\n'
            b'AGENT_VERSION = "0.3.3"\n'
            b'def main():\n'
            b'    print("updated")\n'
            b'if __name__ == "__main__":\n'
            b'    main()\n'
        )
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.code = 200
        mock_resp.read.return_value = new_code
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        handler = object.__new__(agent.AgentHandler)
        handler.agent_token = "perm-token"
        handler.headers = {"Authorization": "Bearer perm-token"}
        handler.path = "/api/update"
        sent = []
        handler.send_json = lambda status, body: sent.append((status, body))

        with mock.patch("agent.Path") as mock_path:
            def path_side_effect(arg):
                if str(arg) in ("/usr/local/lib/dashboard-agent/agent.py", str(self.agent_file)):
                    return self.agent_file
                return Path(arg)
            mock_path.side_effect = path_side_effect

            handler.do_POST()

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 200)
        self.assertEqual(sent[0][1]["status"], "updated")
        self.assertEqual(sent[0][1]["from_version"], "0.3.2")
        self.assertEqual(sent[0][1]["to_version"], "0.3.3")
        self.assertEqual(self.agent_file.read_bytes(), new_code)

    @mock.patch("urllib.request.urlopen")
    def test_update_rejects_syntax_errors(self, mock_urlopen):
        invalid_code = (
            b'#!/usr/bin/env python3\n'
            b'# Invalid Syntax Test File for Dashboard Agent Update\n'
            b'AGENT_VERSION = "0.3.1"\n'
            b'def broken(:\n'
            b'    pass\n'
        )
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.code = 200
        mock_resp.read.return_value = invalid_code
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        handler = object.__new__(agent.AgentHandler)
        handler.agent_token = "perm-token"
        handler.headers = {"Authorization": "Bearer perm-token"}
        handler.path = "/api/update"
        sent = []
        handler.send_json = lambda status, body: sent.append((status, body))

        with mock.patch("agent.Path") as mock_path:
            def path_side_effect(arg):
                if str(arg) in ("/usr/local/lib/dashboard-agent/agent.py", str(self.agent_file)):
                    return self.agent_file
                return Path(arg)
            mock_path.side_effect = path_side_effect

            handler.do_POST()

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 400)
        self.assertIn("verification failed", sent[0][1]["error"])
        # Original file must remain untouched
        self.assertIn('AGENT_VERSION = "0.3.0"', self.agent_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

