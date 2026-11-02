#!/usr/bin/env bash
# Dumps the production database to /var/backups/camino and copies it to S3.
#
#   camino-db-dump nightly       # the systemd timer, once a day
#   camino-db-dump pre-deploy    # by hand, before every deploy
#
# Run it from the repository checkout, as the user who runs Compose. Runbook:
# docs/deploy.md, "Backups". It installs as /usr/local/bin/camino-db-dump, so
# a rollback that checks out an older commit cannot take it away.
#
# The dump keeps every table's schema but leaves out the rows of the index
# cache (code chunks, embeddings, and the index state that points at them);
# re-ingesting rebuilds them. pg_dump runs inside the db container over its
# local socket, so no password is read or passed here.
#
# Settings (environment overrides, mostly for rehearsals):
#   CAMINO_BACKUP_DIR        where dumps go (default /var/backups/camino)
#   CAMINO_BACKUP_CONFIG     S3 settings file (default /etc/camino/backup.conf):
#                              CAMINO_BACKUP_S3_URI=s3://<bucket>/<prefix>, or
#                              none to keep dumps on this box only
#                              CAMINO_BACKUP_S3_REGION=<region>
#   CAMINO_COMPOSE_ENV_FILE  Compose --env-file (default /etc/camino/compose.env)
#   CAMINO_COMPOSE_FILE      Compose file(s), colon-separated
#                            (default docker-compose.prod.yml)
#   CAMINO_BACKUP_LOCK_WAIT  seconds to wait for a running dump (default 900)
set -euo pipefail
umask 077

die() {
  echo "camino-db-dump: $*" >&2
  exit 1
}

label=${1:-}
case $label in
  nightly) keep=14 ;;
  pre-deploy) keep=3 ;;
  *)
    echo "usage: camino-db-dump nightly|pre-deploy" >&2
    exit 2
    ;;
esac

backup_dir=${CAMINO_BACKUP_DIR:-/var/backups/camino}
config=${CAMINO_BACKUP_CONFIG:-/etc/camino/backup.conf}
compose_args=(--env-file "${CAMINO_COMPOSE_ENV_FILE:-/etc/camino/compose.env}")
IFS=: read -ra compose_files <<<"${CAMINO_COMPOSE_FILE:-docker-compose.prod.yml}"
for file in "${compose_files[@]}"; do
  [[ -f $file ]] || die "no $file here; run this from the repository checkout"
  compose_args+=(-f "$file")
done

compose() {
  docker compose "${compose_args[@]}" "$@"
}

# The last KEY=value line wins. The file is read, never sourced.
setting() {
  local value=""
  if [[ -r $config ]]; then
    value=$(sed -n "s/^$1=//p" "$config" | tail -n 1)
  fi
  printf '%s' "$value"
}

s3_uri=$(setting CAMINO_BACKUP_S3_URI)
s3_uri=${s3_uri%/}
s3_region=$(setting CAMINO_BACKUP_S3_REGION)
case $s3_uri in
  none) ;;
  s3://?*) [[ -n $s3_region ]] || die "set CAMINO_BACKUP_S3_REGION in $config" ;;
  *) die "set CAMINO_BACKUP_S3_URI in $config to s3://<bucket>/<prefix>, or to none to keep dumps on this box only" ;;
esac

[[ -d $backup_dir && -w $backup_dir ]] ||
  die "$backup_dir must be a directory you can write to (docs/deploy.md, \"Backups\")"

# One dump at a time. A deploy's pre-deploy dump waits for a nightly one.
exec 9>"$backup_dir/.lock"
flock --wait "${CAMINO_BACKUP_LOCK_WAIT:-900}" 9 || die "another dump is still running"

# Holding the lock, nothing else is writing here: whatever an interrupted run
# left behind (a temp file, or half of a dump/checksum pair) is safe to remove.
shopt -s nullglob
rm -f -- "$backup_dir"/.tmp-*
for file in "$backup_dir"/camino-*.dump; do
  [[ -e $file.sha256 ]] || rm -f -- "$file"
done
for file in "$backup_dir"/camino-*.dump.sha256; do
  [[ -e ${file%.sha256} ]] || rm -f -- "$file"
done

# Captured first: grep -q exiting early could fail the pipe under pipefail.
running=$(compose ps --status running --services)
grep -qx db <<<"$running" || die "the db service is not running; start the stack first"

name="camino-$(date -u +%Y%m%dT%H%M%SZ)-$label.dump"
final="$backup_dir/$name"
[[ ! -e $final && ! -e $final.sha256 ]] || die "$name already exists"

tmp=$(mktemp "$backup_dir/.tmp-$name.XXXXXX")
trap 'rm -f -- "$tmp" "$tmp.sha256"' EXIT
# Turn a stop signal into a normal exit so the EXIT trap still runs.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# -T: no terminal, so the binary archive passes through unchanged.
compose exec -T db pg_dump \
  --no-password --host=/var/run/postgresql --username=camino --dbname=camino \
  --format=custom --lock-wait-timeout=30s \
  --exclude-table-data=public.code_chunks \
  --exclude-table-data=public.code_chunk_embeddings \
  --exclude-table-data=public.repo_index_state \
  >"$tmp" || die "pg_dump failed; earlier dumps are untouched"
[[ -s $tmp ]] || die "pg_dump wrote nothing"

# Decodes every row, not just the table of contents (--list stops there).
compose exec -T db pg_restore --file=/dev/null <"$tmp" ||
  die "the dump does not decode; earlier dumps are untouched"

sum=$(sha256sum -- "$tmp")
sum=${sum%% *}
[[ $sum =~ ^[0-9a-f]{64}$ ]] || die "could not checksum the dump"
printf '%s  %s\n' "$sum" "$name" >"$tmp.sha256"
sync -- "$tmp" "$tmp.sha256"

# ln never replaces an existing file. The dump goes first and the checksum
# last: a dump without its .sha256 is incomplete and is never used.
ln -- "$tmp" "$final" || die "could not publish $name"
if ! ln -- "$tmp.sha256" "$final.sha256"; then
  rm -f -- "$final"
  die "could not publish $name.sha256"
fi
rm -f -- "$tmp" "$tmp.sha256"
sync -- "$backup_dir"
trap - EXIT
echo "camino-db-dump: wrote $final ($(stat -c %s "$final") bytes)" >&2

if [[ $s3_uri != none ]]; then
  for file in "$name" "$name.sha256"; do
    aws s3 cp --only-show-errors --sse AES256 --region "$s3_region" \
      "$backup_dir/$file" "$s3_uri/$file" ||
      die "upload of $file failed; the local copy is kept and no older dump was removed"
  done
  echo "camino-db-dump: uploaded to $s3_uri/$name" >&2
fi

# Keep the newest $keep complete dumps with this label. Names sort by time.
dumps=("$backup_dir"/camino-*-"$label".dump)
complete=0
for ((i = ${#dumps[@]} - 1; i >= 0; i--)); do
  dump=${dumps[i]}
  [[ -e $dump.sha256 ]] || continue
  complete=$((complete + 1))
  if ((complete > keep)); then
    rm -f -- "$dump" "$dump.sha256"
  fi
done
