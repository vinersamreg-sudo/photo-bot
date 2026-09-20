#!/usr/bin/env bash

# Sourceable helpers for validating and atomically updating the Avito release link.

ravuna_avito_inspect_current() {
  local root=$1
  local new_release=$2
  local current=$root/current
  local releases_real new_real target

  RAVUNA_AVITO_CURRENT_STATE=absent
  RAVUNA_AVITO_PREVIOUS_RELEASE=""

  releases_real=$(readlink -e "$root/releases") || {
    echo "Avito releases directory is missing" >&2
    return 1
  }
  new_real=$(readlink -m "$new_release")
  case "$new_real" in
    "$releases_real"/*) ;;
    *) echo "new Avito release is outside releases directory" >&2; return 1 ;;
  esac

  if [ ! -e "$current" ] && [ ! -L "$current" ]; then
    return 0
  fi
  if [ ! -L "$current" ]; then
    echo "Avito current exists but is not a symlink" >&2
    return 1
  fi
  target=$(readlink -e "$current") || {
    echo "Avito current is broken or cyclic" >&2
    return 1
  }
  case "$target" in
    "$releases_real"/*) ;;
    *) echo "Avito current points outside releases directory" >&2; return 1 ;;
  esac
  test -d "$target" || {
    echo "Avito current target is not a release directory" >&2
    return 1
  }

  if [ "$target" = "$new_real" ]; then
    RAVUNA_AVITO_CURRENT_STATE=target
  else
    RAVUNA_AVITO_CURRENT_STATE=previous
    RAVUNA_AVITO_PREVIOUS_RELEASE=$target
  fi
}

ravuna_avito_set_current() {
  local root=$1
  local release=$2
  local current=$root/current
  local temporary=$root/.current.$$
  local release_real

  release_real=$(readlink -e "$release") || {
    echo "Avito release target does not exist" >&2
    return 1
  }
  case "$release_real" in
    "$(readlink -e "$root/releases")"/*) ;;
    *) echo "Avito release target is outside releases directory" >&2; return 1 ;;
  esac
  if [ -e "$current" ] && [ ! -L "$current" ]; then
    echo "refusing to replace non-symlink Avito current" >&2
    return 1
  fi
  rm -f -- "$temporary"
  ln -s -- "$release_real" "$temporary"
  mv -Tf -- "$temporary" "$current"
  test "$(readlink -e "$current")" = "$release_real"
}

ravuna_avito_remove_current() {
  local root=$1
  local expected_release=${2:-}
  local current=$root/current
  local target

  if [ ! -e "$current" ] && [ ! -L "$current" ]; then
    return 0
  fi
  if [ ! -L "$current" ]; then
    echo "refusing to remove non-symlink Avito current" >&2
    return 1
  fi
  if [ -n "$expected_release" ]; then
    target=$(readlink -e "$current") || {
      echo "Avito current cannot be validated before removal" >&2
      return 1
    }
    test "$target" = "$(readlink -e "$expected_release")" || {
      echo "Avito current changed during rollback" >&2
      return 1
    }
  fi
  rm -f -- "$current"
}
