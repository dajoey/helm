# Helm image: one image, two roles (HELM_ROLE=api | web).
#
# Build from the repository root:
#   docker build -f deploy/Dockerfile --build-arg HELM_GIT_SHA=<sha> -t helm:local .
# The context is filtered by deploy/Dockerfile.dockerignore (BuildKit reads the ignore file
# that sits beside the Dockerfile), so .git, databases and env files never enter the build.

# Stage 1 picks the shipped trees. Each tree is copied only if it exists, so the image
# keeps building as directories are added or removed.
FROM python:3.12-slim AS pick
COPY . /src
RUN set -eu; mkdir -p /out; cd /src; \
    for d in app helm learning notebook web tools; do \
      if [ -d "$d" ]; then mkdir -p "/out/$(dirname "$d")"; cp -a "$d" "/out/$d"; fi; \
    done; \
    cp deploy/helm-entrypoint.sh /out/helm-entrypoint.sh; chmod 0755 /out/helm-entrypoint.sh

FROM python:3.12-slim
ARG HELM_GIT_SHA=unknown
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
COPY --from=pick /out/ /app/
# Defaults only; a compose file sets the role, port and paths explicitly.
ENV HELM_ROLE=api HELM_HOST=0.0.0.0 HELM_PORT=3038 \
    HELM_DATA=/data HELM_DB=/data/triage.db HELM_ATTACH_DIR=/data/attachments \
    HELM_BUILD_SHA=${HELM_GIT_SHA}
LABEL org.opencontainers.image.revision="${HELM_GIT_SHA}"
RUN printf '%s\n' "$HELM_BUILD_SHA" > /app/BUILD_SHA
VOLUME /data
EXPOSE 3038
ENTRYPOINT ["/app/helm-entrypoint.sh"]
