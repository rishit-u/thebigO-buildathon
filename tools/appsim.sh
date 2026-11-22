#!/bin/bash
# Lay out /tmp/appsim like the Dockerfile does, build the pinned artifacts, run on a data dir.
set -e
R=/home/claude/thebigo-buildathon; A=/tmp/appsim; D=${1:-$R/sample}
rm -rf $A && mkdir -p $A
cd $R
cp selector.py map.json.gz testmap.json.gz tpcds_fp.py tpcds_res.py maxnodes_fp.py statedigest.py $A/
cp -r rootcfg $A/rootcfg; cp -r tpcds_data $A/tpcds_data
cp runner.py tests.json runner_pinned.json runner_pinned_deps.json tpcds_perq.json $A/
cp $A/runner_pinned_deps.json $A/runner_pinned.json
mkdir -p $A/pinned && cp -r tests_pinned $A/pinned/tests
. /home/claude/work/venv/bin/activate
python $A/selector.py --build-pinned 2>&1 | tail -3
chmod -R a-w $A
cd /tmp && time (BR_DATA=$D HOME=/tmp python $A/selector.py > /tmp/appsim.jsonl 2> /tmp/appsim.err)
chmod -R u+w $A
