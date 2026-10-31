# Gary — AI Game Master (ARCHITECTURE.md §11)
#
# One always-on process holding a Discord Gateway WebSocket. That is the whole shape of
# the runtime, and it is why this is a long-running container rather than anything
# request-driven: Gary listens to ordinary channel messages, which Discord delivers only
# over the Gateway, so a host that suspends between player turns hears nothing.
#
#   docker build -t gary .
#   docker run --env-file .env -v ~/.local/share/gary/campaign:/campaign \
#              ghcr.io/magnentius/gary
#
# The image pins the interpreter, which is the point: every deployment bug this project
# has actually had was the host's Python not being what anyone thought it was.

FROM python:3.12-slim AS build

WORKDIR /src
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

# Build a wheel rather than installing into the final image, so no build tooling and no
# source tree ship to production. `data/*.json` rides along via [tool.setuptools.
# package-data].
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir build \
    && python -m build --wheel --outdir /dist


FROM python:3.12-slim

# The adapters, not [dev]: no pytest in the runtime.
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir --disable-pip-version-check /tmp/*.whl \
        'discord.py>=2.4' 'google-genai>=1.0' \
    && rm -f /tmp/*.whl

# Non-root, and it owns the campaign mount point so a bind mount from the host does not
# land read-only under an unexpected uid.
RUN useradd --create-home --uid 10001 gary \
    && mkdir -p /campaign \
    && chown -R gary:gary /campaign
USER gary

# Inside the image the campaign is always /campaign, overriding the host default of
# ~/.local/share/gary/campaign -- a container has no home directory worth writing to,
# and a fixed path gives the bind mount somewhere obvious to land. Mount a volume here
# or set GARY_BACKEND=firestore; a restart with neither loses the journal, and §7.3 has
# no undo for that.
ENV GARY_CAMPAIGN_ROOT=/campaign \
    PYTHONUNBUFFERED=1
VOLUME ["/campaign"]

# Proves the package imports with the adapters it needs and its class and monster data --
# a wheel missing any of them fails here rather than at the first `?dm new` or fight.
HEALTHCHECK --interval=5m --timeout=10s --start-period=30s \
    CMD python -c "import gary.adapters.discord_bot, gary.sheets, gary.monsters, gary.gear, gary.class_moves, gary.spells, gary.treasure"

ENTRYPOINT ["python", "-m", "gary.adapters.discord_bot"]
