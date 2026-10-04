#!/usr/bin/env python3
"""Resumable NFL receiving data collection. No training or betting logic."""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = 'https://v1.american-football.api-sports.io'
FIELDS = ['season', 'game_id', 'game_date', 'week', 'team_id', 'team_name',
          'opponent_id', 'home', 'player_id', 'player_name', 'targets',
          'receptions', 'receiving_yards', 'receiving_touchdowns']


class CollectionError(Exception):
    pass


def number(value):
    if value is None or str(value).strip() in ('', '-', 'null'):
        return None
    try:
        result = float(value)
        return int(result) if result.is_integer() else result
    except (TypeError, ValueError):
        return None


def parse_receivers(response, game, season):
    if not isinstance(response, list) or len(response) != 2:
        raise CollectionError('Expected two team entries; game needs review')
    expected = {str(game['teams'][side]['id']) for side in ('home', 'away')}
    if {str(x.get('team', {}).get('id')) for x in response} != expected:
        raise CollectionError('Statistics team IDs do not match the schedule')
    rows, seen = [], set()
    for entry in response:
        team = entry['team']
        home = str(team['id']) == str(game['teams']['home']['id'])
        opponent = game['teams']['away' if home else 'home']['id']
        groups = [g for g in entry.get('groups', []) if str(g.get('name', '')).lower() == 'receiving']
        if len(groups) != 1 or not groups[0].get('players'):
            raise CollectionError('Missing receiving group; game needs review')
        for item in groups[0]['players']:
            player = item.get('player', {})
            stats = {str(s.get('name', '')).lower(): s.get('value') for s in item.get('statistics', [])}
            targets, catches = number(stats.get('targets')), number(stats.get('total receptions'))
            if player.get('id') is None or targets is None or catches is None:
                raise CollectionError('Missing player ID, targets or receptions')
            if targets < 0 or catches < 0 or catches > targets or int(targets) != targets or int(catches) != catches:
                raise CollectionError('Invalid target/reception counts')
            identity = (str(team['id']), str(player['id']))
            if identity in seen:
                raise CollectionError('Duplicate player within team')
            seen.add(identity)
            date = game['game'].get('date', {})
            rows.append(dict(zip(FIELDS, [season, game['game']['id'],
                str(date.get('date', '')) + 'T' + str(date.get('time', '')),
                game['game'].get('week'), team['id'], team.get('name'), opponent,
                int(home), player['id'], player.get('name'), int(targets), int(catches),
                number(stats.get('yards')), number(stats.get('receiving touch downs'))])))
    return rows


class Client:
    def __init__(self, reserve):
        self.key = os.environ.get('API_SPORTS_KEY', '')
        self.calls, self.last, self.remaining, self.reserve = 0, 0, None, reserve
        if not self.key:
            raise CollectionError('Missing API_SPORTS_KEY repository secret')

    def get(self, path, **params):
        if self.remaining is not None and self.remaining <= self.reserve:
            raise CollectionError('Stopped at daily quota reserve; resume after reset')
        delay = 7 - (time.monotonic() - self.last)
        if delay > 0:
            time.sleep(delay)
        self.last = time.monotonic()
        self.calls += 1
        req = urllib.request.Request(BASE + path + '?' + urllib.parse.urlencode(params),
                                     headers={'x-apisports-key': self.key})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                value = r.headers.get('x-ratelimit-requests-remaining')
                if value is not None and value.isdigit():
                    self.remaining = int(value)
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise CollectionError(f'HTTP {e.code}; collection stopped without retry') from None
        except Exception:
            raise CollectionError('Network failure or invalid JSON; collection stopped') from None
        if data.get('errors'):
            # Redact credentials, truncate, and avoid URLs from provider messages.
            error = json.dumps(data['errors']).replace(self.key, '[REDACTED]')
            if 'http' in error.lower():
                error = 'Provider API error; inspect dashboard'
            raise CollectionError(error[:350])
        if not isinstance(data.get('response'), list):
            raise CollectionError('Unexpected response schema')
        return data['response']


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.replace(path)


def collect(client, root, season, max_games):
    folder = root / str(season)
    schedule_path = folder / 'schedule.json'
    if schedule_path.exists():
        games = json.loads(schedule_path.read_text())
    else:
        games = client.get('/games', league=1, season=season)
        games = [g for g in games if str(g.get('game', {}).get('stage', '')).lower() in
                 ('regular season', 'regular-season', 'regular')
                 and g['game'].get('status', {}).get('short') in ('FT', 'AOT')]
        if not games:
            raise CollectionError('No completed regular-season games; inspect season/stage access')
        games.sort(key=lambda g: g['game'].get('date', {}).get('timestamp', 0))
        save(schedule_path, games)
    review_path = folder / 'needs_review.json'
    review = json.loads(review_path.read_text()) if review_path.exists() else {}
    fetched = 0
    for game in games:
        gid = str(game['game']['id'])
        path = folder / 'games' / (gid + '.json')
        if path.exists() or gid in review:
            continue
        if fetched >= max_games:
            break
        response = client.get('/games/statistics/players', id=gid)
        fetched += 1
        try:
            rows = parse_receivers(response, game, season)
        except CollectionError as e:
            review[gid] = str(e)
            save(review_path, review)
            continue
        save(path, {'game_id': gid, 'retrieved_at': datetime.now(timezone.utc).isoformat(), 'rows': rows})
    return fetched


def export(root, season):
    folder = root / str(season)
    rows = []
    for path in sorted((folder / 'games').glob('*.json')):
        rows.extend(json.loads(path.read_text())['rows'])
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'receiving_history.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    schedule = folder / 'schedule.json'
    total = len(json.loads(schedule.read_text())) if schedule.exists() else 0
    review = folder / 'needs_review.json'
    reviewed = len(json.loads(review.read_text())) if review.exists() else 0
    completed = len(list((folder / 'games').glob('*.json')))
    return {'season': season, 'schedule_games': total, 'collected_games': completed,
            'needs_review_games': reviewed, 'pending_games': total - completed - reviewed,
            'receiver_rows': len(rows), 'complete_without_review': total > 0 and completed == total}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--season', type=int, choices=[2022, 2023, 2024], default=2024)
    parser.add_argument('--max-games', type=int, default=70)
    parser.add_argument('--reserve', type=int, default=15)
    parser.add_argument('--output', default='nfl/props/history_v1')
    args = parser.parse_args()
    if not 1 <= args.max_games <= 70 or not 10 <= args.reserve <= 99:
        parser.error('max-games must be 1–70; reserve must be 10–99')
    root, error, client = Path(args.output), None, None
    try:
        client = Client(args.reserve)
        collect(client, root, args.season, args.max_games)
    except CollectionError as e:
        error = str(e)
    except Exception:
        error = 'Unexpected data shape; stopped safely for parser review'
    report = export(root, args.season)
    report.update(error=error, calls_this_run=client.calls if client else 0,
                  daily_remaining=client.remaining if client else None)
    save(root / 'collection_report.json', report)
    summary = '# NFL receiving collection\n\n' + '\n'.join(f'- {k}: {v}' for k, v in report.items())
    summary += '\n\nMissing players are not automatically zero-target games. No model or edge claim.\n'
    (root / 'SUMMARY.md').write_text(summary)
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as h:
            h.write(summary)
    return 1 if error else 0


if __name__ == '__main__':
    sys.exit(main())
