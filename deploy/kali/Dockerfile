FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS executor-python
FROM kalilinux/kali-rolling@sha256:6ec743d9fc5cd397451eeddeff3869bcfab5de9bd5dec5b1f34cbfbd80313073
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 PYTHONUNBUFFERED=1
ARG KALI_MIRROR=http://http.kali.org/kali
RUN sed -i "s|http://http.kali.org/kali|${KALI_MIRROR}|g" /etc/apt/sources.list.d/* \
    && apt-get update && apt-get install -y --no-install-recommends \
    openssh-server python3 python3-venv ca-certificates chromium \
    bash curl wget git procps iproute2 netcat-openbsd socat \
    nmap sqlmap gdb binutils file gcc libc6-dev make patchelf checksec \
    python3-pwntools python3-ropgadget unzip zip \
    && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_* \
    && useradd -m -s /bin/bash kali && passwd -d kali \
    && mkdir -p /opt/rexak-executor /run/sshd
WORKDIR /opt/rexak-executor
COPY --from=executor-python /usr/local /usr/local
RUN ldconfig && test "$(id -u kali)" = 1000 && ln -s /usr/bin/python3 /usr/local/bin/pwn-python
COPY deploy/executor-requirements.txt /tmp/executor-requirements.txt
RUN python3 -c "import sys; assert (3,12) <= sys.version_info[:2] < (3,14), sys.version" \
    && python3 -m venv .venv \
    && .venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/executor-requirements.txt
COPY executor/ /tmp/executor/
RUN .venv/bin/pip install --no-cache-dir --no-deps /tmp/executor && rm -rf /tmp/executor
COPY deploy/executor_control.py executor-control.py
COPY deploy/kali/entrypoint.py /usr/local/lib/rexak-entrypoint.py
COPY deploy/kali/sshd_config /etc/ssh/sshd_config
RUN chmod 755 /opt/rexak-executor
EXPOSE 22
ENTRYPOINT ["python3", "/usr/local/lib/rexak-entrypoint.py"]
