@echo off
REM Publishes a read-only dashboard snapshot to bkesari.com. Run by Task Scheduler every 5 minutes.
cd /d "%~dp0"
python publish_snapshot.py >> atip_data\publish.log 2>&1
