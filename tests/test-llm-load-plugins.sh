#!/bin/bash
# This should only run in environemnts where both
# llm-cluster and llm-sentence-transformers are installed

PLUGINS=$(llm plugins)
echo "$PLUGINS" | jq 'any(.[]; .name == "llm-sentence-transformers")' | \
  grep -q true || ( \
    echo "Test failed: llm-sentence-transformers not found" && \
    exit 1 \
  )
# With the LLM_LOAD_PLUGINS we should not see that
PLUGINS2=$(LLM_LOAD_PLUGINS=llm-cluster llm plugins)
echo "$PLUGINS2" | jq 'any(.[]; .name == "llm-sentence-transformers")' | \
  grep -q false || ( \
    echo "Test failed: llm-sentence-transformers should not have been loaded" && \
    exit 1 \
  )
echo "$PLUGINS2" | jq 'any(.[]; .name == "llm-cluster")' | \
  grep -q true || ( \
    echo "Test llm-cluster should have been loaded" && \
    exit 1 \
  )
# With LLM_LOAD_PLUGINS='' we should see no plugins
PLUGINS3=$(LLM_LOAD_PLUGINS='' llm plugins)
echo "$PLUGINS3"| \
  grep -q '\[\]' || ( \
    echo "Test failed: plugins should have returned []" && \
    exit 1 \
  )
