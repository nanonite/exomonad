FROM node:lts
COPY just /usr/local/bin/just
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/seed-venv \
 && /opt/seed-venv/bin/python -m pip download --dest /opt/wheelhouse \
      'pytest>=8' 'mypy==2.3.0' 'ruff==0.16.3' 'setuptools>=68' wheel pip \
 && printf '[global]\nno-index = true\nfind-links = /opt/wheelhouse\n' > /etc/pip.conf \
 && rm -rf /opt/seed-venv \
 && git clone --bare --branch v4 --single-branch https://code.forgejo.org/actions/checkout.git /opt/mirror/actions-checkout.git
