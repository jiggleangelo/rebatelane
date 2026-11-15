import http.client, os, pathlib, socket, subprocess, sys, time, unittest

class HostingContractTests(unittest.TestCase):
    def test_contract_starts_and_accepts_public_host(self):
        root = pathlib.Path(__file__).parents[1]
        public_host = "try-rebatelane.manykiln.com"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
        env = os.environ.copy()
        env.update({"DEMO_HOST": "127.0.0.1", "PORT": str(port), "PUBLIC_BASE_URL": f"https://{public_host}", "DEMO_HTTPS_TERMINATED": "true"})
        env.pop("REBATE_RAIL_ALLOW_HOSTED", None)
        process = subprocess.Popen([sys.executable, "demo.py"], cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline, last_error = time.monotonic() + 5, None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    self.fail(f"server exited early ({process.returncode})\n{stdout}\n{stderr}")
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=.25)
                try:
                    connection.request("GET", "/healthz", headers={"Host": public_host})
                    response = connection.getresponse()
                    self.assertEqual((response.status, response.read()), (200, b"ok")); break
                except OSError as error:
                    last_error = error; time.sleep(.05)
                finally: connection.close()
            else: self.fail(f"server did not become ready: {last_error}")
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(2)
                except subprocess.TimeoutExpired: process.kill(); process.wait(2)

if __name__ == "__main__": unittest.main()
