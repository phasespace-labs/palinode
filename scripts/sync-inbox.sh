#!/bin/bash
# Sync MBP Palinode inbox to your-server
# Run as cron on your-server every 2 minutes:
#   */2 * * * * /path/to/palinode/scripts/sync-inbox.sh

MBP_HOST="user@your-server"
MBP_INBOX="~/Palinode-Inbox/"
LOCAL_INBOX="/path/to/palinode/inbox/raw/"

# Only sync if MBP is reachable (2s timeout)
if ssh -o ConnectTimeout=2 -o BatchMode=yes "$MBP_HOST" true 2>/dev/null; then
    rsync -az --remove-source-files "$MBP_HOST:$MBP_INBOX" "$LOCAL_INBOX" 2>/dev/null
    
    # Process any new files
    if [ "$(ls -A $LOCAL_INBOX 2>/dev/null)" ]; then
        cd /path/to/palinode
        source venv/bin/activate
        python3 -m palinode.ingest.pipeline
    fi
fi
