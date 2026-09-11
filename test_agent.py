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


if __name__ == "__main__":
    unittest.main()
