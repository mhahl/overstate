# Example reactor: greet newly started minions. Copy and adapt; the
# Reactor page lists whatever the master reports via reactor.list.
greet-new-minion:
  local.state.apply:
    - tgt: {{ data['id'] }}
    - tgt_type: list
    - arg:
      - greeting
