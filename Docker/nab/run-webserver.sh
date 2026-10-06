#!/bin/bash
exec /opt/venv/bin/gunicorn --workers 1 --threads 4 --timeout 60 -b 0.0.0.0 nabweb.wsgi
