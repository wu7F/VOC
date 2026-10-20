#!/usr/bin/env bash
# Stage 7 第一次端到端运行：用编排器复跑既有的 Reddit 分析（参数精确复刻既有 stats.json）
# 目的：证明编排器没有改变任何一步的行为 —— clean 产物应与 data/reddit_analysis/ 逐字节一致。
set -u
cd "$(dirname "$0")/.." || exit 1

REPO="C:/Users/Administrator/WorkBuddy/2026-10-06-10-51-11"
DA="$REPO/data/reddit_analysis"

python voc.py run \
  --input "$DA/reddit_posts.csv" \
  --comments "$DA/reddit_comments.csv" \
  --category "Nursery Glider Recliner" \
  --keywords "nursery glider,nursery chair,glider,recliner,rocker,swivel glider,nursing chair,feeding chair,nursery,baby room,gliding,reclining" \
  --brands "Babyletto,La-Z-Boy,CHITA,Pottery Barn Kids,DaVinci,Namesake,Nurture&,Dutailier,Storkcraft,Delta Children,IKEA,Dream On Me,Baby Relax,Westwood,Evolur,Best Chairs,Carter's" \
  --max-age-years 2.0 \
  --profile reddit \
  --job-dir out/verify/stage7/reddit \
  --mock
echo "EXIT=$?"
