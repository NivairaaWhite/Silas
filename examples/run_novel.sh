#!/usr/bin/env bash
# Example: scrape a novel with storage limits
silas \
  -u "https://novellive.app/book/my-reincarnation-as-a-chicken" \
  --mode novel \
  --max-chapters 20 \
  --max-mb 10 \
  -o ./out_novel
