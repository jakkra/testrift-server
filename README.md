## TestRift Server (`testrift-server`)

Python server for TestRift real-time test runs: live log streaming, result storage, and a web UI for browsing and analysis.

![Tests](https://github.com/testrift/testrift-server/actions/workflows/tests.yml/badge.svg)

### Experimental

TestRift is currently in an **experimental** phase. APIs, configuration, and data formats may change at any time **without notice**.

### Install

```bash
pip install testrift-server
```

### Run

```bash
testrift-server
```

Or:

```bash
python -m testrift_server
```

### Development

Run the Python test suite:

```bash
inv test
```

Run the browser-side static asset test suite (Jest, for files under
`src/testrift_server/static/`):

```bash
inv test-js
```

### Docker

All Docker-related files live in `docker/`.

**Build the image:**

```bash
docker build --network=host -f docker/Dockerfile -t testrift-server:latest .
```

**Run a container** (host networking — the server binds directly to host port 8080):

```bash
docker run -d \
  --name testrift-server \
  --network=host \
  -v testrift-data:/data \
  testrift-server:latest
```

**Using Docker Compose** (from the project root):

```bash
docker compose -f docker/docker-compose.yml up -d
```

Or from the `docker/` directory:

```bash
cd docker && docker compose up -d
```

The server will be available at `http://localhost:8080`. Data is persisted in the `testrift-data` Docker volume.

To use a custom config, mount it over the default one:

```bash
docker run -d \
  --name testrift-server \
  --network=host \
  -v /path/to/your/testrift_server.yaml:/app/testrift_server.yaml:ro \
  -v testrift-data:/data \
  testrift-server:latest
```

Or set the `TESTRIFT_SERVER_YAML` environment variable to an absolute path inside the container.

### Configuration

- The server loads configuration from either:
  - `testrift_server.yaml` in the directory you run `testrift-server` from, or
  - `TESTRIFT_SERVER_YAML` (a filesystem path to a YAML config file; absolute path recommended). If set, the server will **fail to start** if the file does not exist.

For the full configuration reference, see [server_config.md](docs/server_config.md).

Login, roles, and the Users page: [authentication.md](docs/authentication.md).

TLS for test clients (automatic CA, fingerprint pinning): [tls.md](docs/tls.md).

HTTP/WebSocket path contracts (frozen for clients): [http_contracts.md](docs/http_contracts.md).

KPI measurements, uploads, history, and comparisons: [kpis.md](docs/kpis.md).

Comments on runs and test case logs: [comments.md](docs/comments.md).


