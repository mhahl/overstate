#!/bin/bash
set -e

# Record initial activity
date +%s > /tmp/last_activity

# Start idle watcher in background (15 minutes idle timeout)
(
    TIMEOUT=900
    while true; do
        sleep 30
        NOW=$(date +%s)
        LAST=$(cat /tmp/last_activity 2>/dev/null || echo $NOW)
        DIFF=$((NOW - LAST))
        if [ $DIFF -ge $TIMEOUT ]; then
            echo "salt-cli pod idle timeout ($TIMEOUT s reached). Exiting."
            kill -TERM 1 2>/dev/null || exit 0
            exit 0
        fi
    done
) &

exec "$@"
