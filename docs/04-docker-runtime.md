# Docker Runtime

## Goal

Run the Threads automation stack fully inside Docker, while keeping the project source mounted into the container.

This Docker setup is required because the old local workflows depended on:

- Windows paths like `D:\Project III\n8n-ai-video\...`
- `Code` nodes using Node `child_process`
- a local Python venv outside the container

The Docker-compatible workflows now use:

- `Execute Command` nodes
- Linux path `/workspace`
- Python and ffmpeg installed in the custom n8n image
- mounted project source from the repo root

## Files

- `Dockerfile`
- `docker-compose.yml`
- `requirements-docker.txt`

## Start

From the repo root:

```bash
docker compose down
docker compose up --build -d
```

Then open:

```text
http://localhost:5678
```

## Important Mounts

The compose file mounts:

- project source to `/workspace`
- n8n data to `/home/node/.n8n`

That means the container can run:

```text
python3 /workspace/src/threads_miner.py
python3 /workspace/src/screenshot_extractor.py
python3 /workspace/src/video_factory.py
```

## Workflow Files To Import

Re-import these updated workflow JSON files after rebuilding the Docker stack:

- `workflows/01-threads-miner.json`
- `workflows/02-screenshot-extract.json`
- `workflows/03-video-maker.json`

## Notes

- Phase 1 and Phase 2 use Playwright with system Chromium in the container.
- Phase 3 uses ffmpeg from the container image.
- `.env` is still read from the repo root and passed into the container with `env_file`.
- Existing n8n credentials are stored in the `n8n_data` volume, not in the repo.
