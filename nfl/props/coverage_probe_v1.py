#!/usr/bin/env python3
"""Manual data audit only. No training, predictions, bets, retries or raw responses."""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


class ProbeError(Exception):
    pass


class Client:
    def __init__(self, base, secret, header=False):
        self.base, self.key = base, os.environ.get(secret, '')
        self.header, self.calls, self.quota = header, 0, {}

    def get(self, path, **params):
        if not self.key:
            raise ProbeError('Missing GitHub secret')
        if self.calls:
            time.sleep(1.1)
        headers = {}
        if self.header:
            headers['x-apisports-key'] = self.key
        else:
            params['apiKey'] = self.key
        self.calls += 1
        req = urllib.request.Request(self.base + path + '?' + urllib.parse.urlencode(params), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                for name in ('x-requests-remaining', 'x-requests-used', 'x-requests-last',
                             'x-ratelimit-requests-remaining', 'x-ratelimit-requests-limit'):
                    value = response.headers.get(name)
                    if value is not None:
                        self.quota[name] = value
                data = json.load(response)
        except urllib.error.HTTPError as error:
            raise ProbeError(f'HTTP {error.code}; check plan, credentials or endpoint') from None
        except Exception:
            # Never print exception text: HTTP errors/URLs may include keys.
            raise ProbeError('Network failure or invalid JSON') from None
        if isinstance(data, dict) and (data.get('errors') or data.get('error')):
            raise ProbeError('Provider returned an API error; inspect its dashboard')
        return data


def array(data):
    if not isinstance(data, list):
        raise ProbeError('Unexpected response shape: expected list')
    return data


def future(value, now):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')) > now
    except (ValueError, TypeError, AttributeError):
        return False


def odds_api(client, now):
    events = array(client.get('/sports/americanfootball_nfl/events'))
    events = sorted((x for x in events if future(x.get('commence_time'), now)), key=lambda x: x['commence_time'])
    if not events:
        return {'status': 'NO_UPCOMING_GAME'}
    event = events[0]
    data = client.get('/sports/americanfootball_nfl/events/' + str(event['id']) + '/odds',
                      regions='us', markets='player_receptions', oddsFormat='decimal')
    rows = []
    for book in data.get('bookmakers', []):
        for market in book.get('markets', []):
            if market.get('key') != 'player_receptions':
                continue
            for outcome in market.get('outcomes', []):
                rows.append({'book': book.get('key'), 'player': outcome.get('description'),
                             'side': outcome.get('name'), 'line': outcome.get('point'),
                             'decimal_price': outcome.get('price'), 'updated_at': market.get('last_update')})
    return {'status': 'QUOTES_FOUND' if rows else 'NO_QUOTES_FOR_SAMPLED_GAME',
            'game': f"{event.get('away_team')} @ {event.get('home_team')}",
            'kickoff': event['commence_time'], 'receptions_quotes': rows}


def oddspapi(client, now):
    markets = array(client.get('/markets', language='en'))
    rec = {str(x['marketId']): x for x in markets if str(x.get('sportId')) == '14'
           and x.get('playerProp') and 'reception' in str(x.get('marketName', '')).lower()}
    fixtures = array(client.get('/fixtures', sportId=14, statusId=0,
                                **{'from': now.date().isoformat(),
                                   'to': (now + timedelta(days=7)).date().isoformat()}))
    fixtures = sorted((x for x in fixtures if str(x.get('tournamentName', '')).upper() == 'NFL'
                       and x.get('hasOdds') and future(x.get('startTime'), now)), key=lambda x: x['startTime'])
    if not fixtures:
        return {'status': 'NO_PRICED_NFL_GAME_IN_7_DAYS', 'catalogue_reception_markets': len(rec)}
    event = fixtures[0]
    data = client.get('/odds', fixtureId=event['fixtureId'], oddsFormat='decimal', verbosity=3)
    rows = []
    for book, info in data.get('bookmakerOdds', {}).items():
        if info.get('bookmakerIsActive') is False or info.get('suspended'):
            continue
        for mid, market in info.get('markets', {}).items():
            if mid not in rec or market.get('marketActive') is False:
                continue
            meta = rec[mid]
            labels = {str(x['outcomeId']): x.get('outcomeName') for x in meta.get('outcomes', [])}
            for oid, outcome in market.get('outcomes', {}).items():
                for player in outcome.get('players', {}).values():
                    if player.get('active') is False or not player.get('playerName'):
                        continue
                    rows.append({'book': book, 'player': player.get('playerName'),
                                 'market': meta.get('marketName'), 'side': labels.get(oid),
                                 'line': meta.get('handicap'), 'decimal_price': player.get('price'),
                                 'updated_at': player.get('changedAt')})
    return {'status': 'QUOTES_FOUND' if rows else 'NO_QUOTES_FOR_SAMPLED_GAME',
            'game': f"{event.get('participant1Name')} vs {event.get('participant2Name')}",
            'kickoff': event['startTime'], 'catalogue_reception_markets': len(rec), 'receptions_quotes': rows}


def stat_fields(data):
    fields = set()
    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if any(term in key.lower() for term in ('recept', 'target', 'receiv', 'catch')):
                    fields.add(key)
                if key in ('name', 'type', 'group') and isinstance(child, str) and any(
                        term in child.lower() for term in ('recept', 'target', 'receiv', 'catch')):
                    fields.add(child)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(data)
    return sorted(fields)


def api_nfl(client, season):
    league = array(client.get('/leagues', id=1, season=season).get('response'))
    if not league:
        return {'status': 'NO_SEASON_ACCESS', 'season': season}
    seasons = league[0].get('seasons', [])
    coverage = next((x.get('coverage', {}) for x in seasons if str(x.get('year')) == str(season)), {})
    games = array(client.get('/games', league=1, season=season).get('response'))
    finished = sorted((x for x in games if x.get('game', {}).get('status', {}).get('short') in ('FT', 'AOT')),
                      key=lambda x: x.get('game', {}).get('date', {}).get('date', ''), reverse=True)
    result = {'status': 'NO_COMPLETED_GAME' if not finished else 'SCHEDULE_FOUND',
              'season': season, 'coverage': coverage, 'games_returned': len(games)}
    flags = coverage.get('games', {})
    stats = flags.get('statistics', flags.get('statisitcs', {}))
    if finished and stats.get('players'):
        game = finished[0]['game']['id']
        data = client.get('/games/statistics/players', id=game)
        fields = stat_fields(data.get('response'))
        result.update(status='PLAYER_STATS_FOUND' if data.get('response') else 'EMPTY_PLAYER_STATS',
                      sampled_game_id=game, receiving_field_labels=fields)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--season', type=int, default=2026)
    parser.add_argument('--output', default='props_probe')
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    clients = {
        'the_odds_api': Client('https://api.the-odds-api.com/v4', 'ODDS_API_KEY'),
        'oddspapi': Client('https://api.oddspapi.io/v4', 'ODDSPAPI_API_KEY'),
        'api_nfl': Client('https://v1.american-football.api-sports.io', 'API_SPORTS_KEY', True)}
    tasks = {'the_odds_api': lambda c: odds_api(c, now), 'oddspapi': lambda c: oddspapi(c, now),
             'api_nfl': lambda c: api_nfl(c, args.season)}
    report = {'checked_at': now.isoformat(), 'purpose': 'Coverage audit only; no predictions', 'providers': {}}
    failed = False
    for name, client in clients.items():
        try:
            result = tasks[name](client)
        except ProbeError as error:
            result = {'status': 'ERROR', 'reason': str(error)}
            failed = True
        except Exception:
            result = {'status': 'ERROR', 'reason': 'Unexpected provider schema; review parser'}
            failed = True
        result.update(http_calls=client.calls, quota_headers=client.quota)
        report['providers'][name] = result
        print(name + ': ' + result['status'])
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, indent=2)
    # Defense in depth: providers can echo credentials in unexpected fields.
    for client in clients.values():
        if client.key:
            text = text.replace(client.key, '[REDACTED]')
    (output / 'coverage_report.json').write_text(text + '\n')
    summary = '# NFL receptions coverage check\n\n' + '\n'.join(
        f"- {name}: {result['status']} ({result['http_calls']} HTTP calls)"
        for name, result in report['providers'].items())
    summary += '\n\nSee coverage_report.json for sampled quotes, field labels and quota headers.\n'
    summary += 'No quotes in one game does not establish that a provider never covers receptions.\n'
    (output / 'SUMMARY.md').write_text(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as handle:
            handle.write(summary)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
