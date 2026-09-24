"""Text and menu labels for manual Claude Code usage resets.

Presentation only. No logic, network calls, or imports.
"""

MENU_TITLE = '↺ Reset Claude usage'
CONFIRM_TITLE = 'Reset Claude usage?'

_LIMIT_LABELS = {
    'five_hour': '5-hour limit',
    'seven_day': 'Weekly limit',
    'seven_day_overage_included': 'Weekly included usage limit',
    'seven_day_cowork': 'Weekly Cowork limit',
    'seven_day_omelette': 'Weekly Omelette limit',
    'seven_day_opus': 'Weekly Opus limit',
    'seven_day_sonnet': 'Weekly Sonnet limit',
    'seven_day_oauth_apps': 'Weekly limit for connected apps',
}

_MONTHS = (
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
)

_RESULTS = {
    'reset': 'Your Claude usage was reset. One reset was used.',
    'already_used': 'This reset was already used.',
    'not_limited': 'You have not hit a usage limit, so there is nothing to reset.',
    'cooldown': 'A reset was used recently. Try again later.',
    'ineligible': 'This account can not use usage resets.',
    'unavailable': 'No reset is available for this account right now.',
    'changed': 'The reset details changed. Open the menu to check again.',
    'unknown': (
        'We could not confirm what happened. '
        'Check your Claude usage before you try again.'
    ),
    'login_required': 'Sign in to Claude again, then try the reset.',
    'busy': 'A reset is already in progress. Wait for it to finish.',
}


def _plural(count, word):
    return '%d %s' % (count, word if count == 1 else word + 's')


def _limit_label(limit_id):
    label = _LIMIT_LABELS.get(limit_id)
    if label:
        return label
    text = str(limit_id).replace('_', ' ').strip()
    if not text:
        return 'Usage limit'
    text = text[0].upper() + text[1:]
    if 'limit' not in text.lower():
        text += ' limit'
    return text


def _format_date(iso):
    if not iso:
        return None
    text = str(iso)
    try:
        year = int(text[0:4])
        month = int(text[5:7])
        day = int(text[8:10])
    except ValueError:
        return None
    if text[4:5] != '-' or text[7:8] != '-' or not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    date = '%s %d, %d' % (_MONTHS[month - 1], day, year)
    time = text[11:16]
    is_utc = text.endswith('Z') or text.endswith('+00:00')
    if len(time) == 5 and time[2] == ':' and time.replace(':', '').isdigit() and is_utc:
        return '%s at %s UTC' % (date, time)
    return date


def account_title(email, status=None):
    if status is None:
        return f"{email} (checking resets…)"
    remaining = status.remaining
    if remaining is None:
        return f"{email} (could not check)"
    if remaining == 0:
        return f"{email} (0 resets left)"
    noun = "reset" if remaining == 1 else "resets"
    availability = "unavailable now" if status.offer is None else "available"
    return f"{email} ({remaining} {noun} left, {availability})"

def confirmation(offer):
    lines = ['Account: %s' % offer.email]
    if offer.label:
        lines.append('Reset: %s' % offer.label)
    clears = [_limit_label(limit_id) for limit_id in (offer.clears or ())]
    if clears:
        lines.append('Clears: %s' % ', '.join(clears))
    if offer.total_remaining is not None:
        lines.append('Resets left: %d' % offer.total_remaining)
    ends = _format_date(offer.ends_at)
    if ends:
        lines.append('Offer ends: %s' % ends)
    lines.append('')
    if offer.total_remaining:
        left_after = max(offer.total_remaining - 1, 0)
        lines.append(
            'This uses 1 reset and leaves you with %s. You can not undo it.'
            % _plural(left_after, 'reset')
        )
    else:
        lines.append('This uses 1 reset. You can not undo it.')
    return '\n'.join(lines)


def unavailable(email, remaining):
    if remaining is None:
        return '%s\nWe could not check your resets right now.' % email
    if remaining <= 0:
        return '%s\nNo resets left.' % email
    return '%s\n%s left. None can be used right now.' % (
        email,
        _plural(remaining, 'reset'),
    )


def result_message(code):
    return _RESULTS.get(code, _RESULTS['unknown'])