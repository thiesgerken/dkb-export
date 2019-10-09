#!/usr/bin/env python3
# DKB Credit and Giro Exporter
# Copyright (C) 2019 Thies Gerken <thies@thiesgerken.de>
#
# Based on DKB Credit card transaction QIF exporter <https://github.com/hoffie/dkb-visa>
# Copyright (C) 2013 Christian Hoffmann <mail@hoffmann-christian.info>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import unittest
import re
import os
import csv
import sys
import pickle
import logging
import mechanize
import time


class RecordingBrowser(mechanize.Browser):
    _recording_path = None
    _recording_enabled = False
    _playback_enabled = False
    _intercept_count = 0

    def enable_recording(self, path):
        self._recording_path = path
        self._recording_enabled = True

    def enable_playback(self, path):
        self._recording_path = path
        self._playback_enabled = True

    def open(self, *args, **kwargs):
        return self._intercept_call('open', *args, **kwargs)

    def back(self):
        if self._playback_enabled:
            self._intercept_count += 1
            return self._read_recording()
        else:
            mechanize.Browser.back(self)

            if self._recording_enabled:
                self._do_record()

    def _intercept_call(self, method, *args, **kwargs):
        if self._playback_enabled:
            self._intercept_count += 1
            return self._read_recording()

        func = getattr(mechanize.Browser, method)
        ret = func(self, *args, **kwargs)
        if self._recording_enabled:
            self._do_record()
        return ret

    def _do_record(self):
        """
        Writes the current HTML to disk if dumping is enabled.
        Useful for offline testing.
        """
        data = {}
        resp = self.response()
        if not resp:
            return
        data['data'] = resp.get_data()
        data['code'] = resp.code
        data['msg'] = resp.msg
        data['headers'] = resp.info().items()
        data['url'] = resp.geturl()

        self._intercept_count += 1
        dump_path = '%s/%d.json' % (self._recording_path,
                                    self._intercept_count)
        with open(dump_path, 'wb') as f:
            pickle.dump(data, f)

    def _read_recording(self):
        dump_path = '%s/%d.json' % (self._recording_path,
                                    self._intercept_count)
        if not os.path.exists(dump_path):
            return

        with open(dump_path, 'rb') as f:
            data = pickle.load(f)
            if not data:
                self.set_response(None)
                return
            resp = mechanize.make_response(**data)
            return self.set_response(resp)


logger = logging.getLogger(__name__)


class DkbScraper(object):

    def __init__(self, record_html=False, playback_html=False, interactive=True):
        self.br = RecordingBrowser()
        dump_path = os.path.join(os.path.dirname(__file__), 'dumps')
        if record_html:
            if not os.path.exists(dump_path):
                os.mkdir(dump_path)

            self.br.enable_recording(dump_path)
        if playback_html:
            self.br.enable_playback(dump_path)

        self.interactive = interactive

    def login(self, userid, pin):
        """
        Create a new session by submitting the login form

        @param str userid
        @param str pin
        """
        logger.info("Starting login as user %s...", userid)
        br = self.br

        # we are not a spider, so let's ignore robots.txt...
        br.set_handle_robots(False)

        # Although we have to handle a meta refresh, we disable it here
        # since mechanize seems to be buggy and will be stuck in a
        # long (infinite?) sleep() call
        br.set_handle_refresh(False)

        br.open('https://www.dkb.de/-?$javascript=disabled')

        # select login form:
        br.form = list(br.forms())[1]

        br.set_all_readonly(False)
        br.form["j_username"] = userid
        br.form["j_password"] = pin
        br.form["jsEnabled"] = "false"
        br.form["browserName"] = "Firefox"
        br.form["browserVersion"] = "40"
        br.form["screenWidth"] = "1000"
        br.form["screenHeight"] = "800"
        br.form["osName"] = "Windows"
        br.submit()
        self.confirm_login()

    def confirm_login(self):
        br = self.br
        html = br.response().get_data().decode('utf-8')

        if re.search("Anmeldung zum Internet-Banking", html):
            raise RuntimeError("PIN seems to be wrong")

        if re.search("Bestätigen Sie Ihre Anmeldung im Banking zusätzlich mit einer", html):
            if not self.interactive:
                raise RuntimeError(
                    "TAN Required in non-interactive environment")

            logger.info("TAN Required")

            if self._get_tan_input_form() is None:
                # if we don't find the tan field, we're probably at the empty form
                logger.info("Empty TAN form, submitting.")
                br.submit()
                html = br.response().get_data().decode('utf-8')

            form = self._get_tan_input_form()
            br.form = form

            if form is None:
                raise RuntimeError("Could not find TAN form")

            startcode = re.search("Startcode ([0-9]{8})", html)
            if startcode:
                print(f'chipTAN Startcode: {startcode.group(1)}')

            br.form["tan"] = self.ask_for_tan()
            br.submit()

            # if we find the tan field after submitting, the TAN was wrong
            if not self._get_tan_input_form() is None:
                raise RuntimeError("TAN seems to be wrong")

        elif re.search("und bestätigen dort", html):
            device = re.search(
                "Gerät: <strong>([^<>]+)</strong>", html).group(1)
            logger.info(
                f"Waiting for confirmation through app on device '{device}'")

            form = self._get_app_form()
            br.form = form

            if form is None:
                raise RuntimeError("Could not find XSRFPreventionToken form")

            token = br.form["XSRFPreventionToken"]
            logger.info(f"XSRFPreventionToken: {token}")

            timeout = 100
            interval = 2

            while timeout > 0:
                br.open(
                    'https://www.dkb.de/DkbTransactionBanking/content/LoginWithBoundDevice/LoginWithBoundDeviceProcess/confirmLogin.xhtml?$event=pollingVerification')

                html = br.response().get_data().decode('utf-8')

                if re.search("WAITING", html):
                    logger.info(
                        "Polling Verification: Waiting for Confirmation")
                elif re.search("MAP_TO_EXIT", html):
                    logger.info("Polling Verification: Complete")
                    break

                time.sleep(interval)
                timeout -= interval

            if timeout <= 0:
                raise RuntimeError("Timeout for App confirmation")

            request = mechanize.Request(
                'https://www.dkb.de/DkbTransactionBanking/content/LoginWithBoundDevice/LoginWithBoundDeviceProcess/confirmLogin.xhtml')
            br.open(
                request, data=f'$event=next&XSRFPreventionToken={token}')

        br.open("https://www.dkb.de/-?$javascript=disabled")

    def ask_for_tan(self):
        tan = ""
        import os
        if os.isatty(0):
            while not tan.strip():
                tan = input('TAN: ')
        else:
            tan = sys.stdin.read().strip()
        return tan

    def _get_tan_input_form(self):
        """
        Internal.

        Returns the tan input form object (mechanize)
        """
        for form in self.br.forms():
            try:
                form.find_control(name="tan")
                return form
            except Exception:
                continue

        return None

    def _get_app_form(self):
        """
        Internal.

        Returns the tan input form object (mechanize)
        """
        for form in self.br.forms():
            try:
                form.find_control(name="XSRFPreventionToken")
                return form
            except Exception:
                continue

        return None

    def transactions_overview(self):
        """
        Navigates the internal browser state to the credit card
        transaction overview menu
        """
        logger.info("Navigating to 'Umsätze'...")
        try:
            return self.br.follow_link(url_regex='banking/finanzstatus/kontoumsaetze')
        except Exception:
            raise RuntimeError('Unable to find link Umsätze -- '
                               'Maybe the login went wrong?')

    def _get_transaction_selection_form(self):
        """
        Internal.

        Returns the transaction selection form object (mechanize)
        """
        for form in self.br.forms():
            try:
                form.find_control(name="slAllAccounts", type='select')
                return form
            except Exception:
                continue

        raise RuntimeError("Unable to find transaction selection form")

    def _select_all_credit_transactions_from(self, form, from_date, to_date):
        """
        Internal.

        Checks the radio box "Alle Umsätze vom" and populates the
        "from" and "to" with the given values.

        @param mechanize.HTMLForm form
        @param str from_date dd.mm.YYYY
        @param str to_date dd.mm.YYYY
        """
        try:
            radio_ctrl = form.find_control("filterType")
        except Exception:
            raise RuntimeError("Unable to find search period radio box")

        form[radio_ctrl.name] = [u'DATE_RANGE']

        try:
            from_item = form.find_control(label="vom")
        except Exception:
            raise RuntimeError("Unable to find 'vom' date field")

        from_item.value = from_date

        try:
            to_item = form.find_control(label="bis")
        except Exception:
            raise RuntimeError("Unable to find 'to' date field")

        to_item.value = to_date

    def _select_all_giro_transactions_from(self, form, from_date, to_date):
        """
        Internal.

        Checks the radio box "Alle Umsätze vom" and populates the
        "from" and "to" with the given values.

        @param mechanize.HTMLForm form
        @param str from_date dd.mm.YYYY
        @param str to_date dd.mm.YYYY
        """

        try:
            radio_ctrl = form.find_control("searchPeriodRadio")
        except Exception:
            raise RuntimeError("Unable to find search period radio box")

        all_transactions_item = None
        for item in radio_ctrl.items:
            if item.id.endswith(":1"):
                all_transactions_item = item
                break

        if not all_transactions_item:
            raise RuntimeError(
                "Unable to find 'Zeitraum: vom' radio box")

        form[radio_ctrl.name] = ["1"]  # select from/to date, not "all"

        try:
            from_item = form.find_control(name="transactionDate")
        except Exception:
            raise RuntimeError("Unable to find 'vom' (from) date field")

        from_item.value = from_date

        try:
            to_item = form.find_control(name="toTransactionDate")
        except Exception:
            raise RuntimeError("Unable to find 'bis' (to) date field")

        to_item.value = to_date

    def _select_account(self, form, account):
        """
        Internal.

        Selects the correct account (credit or debit) from the dropdown menu in the
        transaction selection form.

        @param mechanize.HTMLForm form
        @param str account: text of the relevant label
        """

        try:
            cc_list = form.find_control(name="slAllAccounts", type='select')
        except Exception:
            raise RuntimeError("Unable to find credit card selection form")

        for item in cc_list.get_items():
            # find right credit card...
            for label in item.get_labels():
                if label.text == account:
                    cc_list.value = [item.name]
                    return

        raise RuntimeError("Unable to find the right account")

    def list_accounts(self):
        """
        List credit cards and debit accounts
        """
        br = self.br
        br.form = form = self._get_transaction_selection_form()

        try:
            cc_list = form.find_control(name="slAllAccounts", type='select')
        except Exception:
            raise RuntimeError("Unable to find credit card selection form")

        c_accs = []
        d_accs = []

        for item in cc_list.get_items():
            for label in item.get_labels():
                if label.text.endswith('Kreditkarte'):
                    c_accs.append(label.text)
                elif label.text.endswith('Girokonto'):
                    d_accs.append(label.text)
                else:
                    print(f'Warning: Unknown account type: \'{label.text}\'')

        return c_accs, d_accs

    def select_credit_card_transactions(self, label, from_date, to_date):
        """
        Changes the current view to show all transactions between
        from_date and to_date for the credit card identified by the
        given card id.

        @param str label: label of the drop down
        @param str from_date dd.mm.YYYY
        @param str to_date dd.mm.YYYY
        """
        br = self.br
        logger.info("Selecting credit card transactions in time frame %s - %s...",
                    from_date, to_date)

        br.form = form = self._get_transaction_selection_form()
        # self._select_credit_card(form, label)
        self._select_account(form, label)
        # we need to reload so that we get the credit card form:
        br.submit()

        br.form = form = self._get_transaction_selection_form()
        self._select_all_credit_transactions_from(form, from_date, to_date)

        # add missing $event control
        br.form.new_control('hidden', '$event', {'value': 'search'})
        br.form.fixup()
        br.submit()

    def select_giro_transactions(self, label, from_date, to_date):
        """
        Changes the current view to show all transactions between
        from_date and to_date for the giro account identified by the
        given label.

        @param str label: label of the drop down
        @param str from_date dd.mm.YYYY
        @param str to_date dd.mm.YYYY
        """
        br = self.br
        logger.info("Selecting giro transactions in time frame %s - %s...",
                    from_date, to_date)

        br.form = form = self._get_transaction_selection_form()
        # self._select_credit_card(form, label)
        self._select_account(form, label)
        # we need to reload so that we get the credit card form:
        br.submit()

        br.form = form = self._get_transaction_selection_form()
        self._select_all_giro_transactions_from(form, from_date, to_date)

        # add missing $event control
        br.form.new_control('hidden', '$event', {'value': 'search'})
        br.form.fixup()
        br.submit()

    def get_transaction_csv(self):
        """
        Returns a file-like object which contains the CSV data,
        selected by previous calls.

        @return file-like response
        """
        logger.info("Requesting CSV data...")
        self.br.follow_link(url_regex='csv')
        return self.br.response().read().decode('latin_1')

    def logout(self):
        """
        Properly ends the session.
        """
        logger.info("Logging out")
        self.br.open("https://www.dkb.de/-?$javascript=disabled")
        self.br.follow_link(text='Abmelden')

    def get_csv_name(self):
        dispo = self.br.response().get('Content-Disposition', '')

        if not dispo.startswith('attachment; filename='):
            raise RuntimeError("Unable to find filename")

        return dispo[len("attachment; filename="):]


if __name__ == '__main__':
    from getpass import getpass
    from argparse import ArgumentParser
    from datetime import date, timedelta

    cli = ArgumentParser()
    cli.add_argument("--userid",
                     help="Your user id (same as used for login)")
    cli.add_argument("--output", "-o", default=".",
                     help="Output directory for csv files")
    cli.add_argument("--from-date",
                     help="Export transactions as of... (DD.MM.YYYY)",
                     default=(date.today() - timedelta(days=180)).strftime('%d.%m.%Y'))
    cli.add_argument("--to-date",
                     help="Export transactions until... (DD.MM.YYYY)",
                     default=date.today().strftime('%d.%m.%Y'))
    cli.add_argument("--debug", action="store_true")
    cli.add_argument("--batch", action="store_true",
                     help="Do not wait for TAN inputs (Only authentication through app possible)")

    args = cli.parse_args()
    if not args.userid:
        cli.error("Please specify a valid user id")

    def is_valid_date(date):
        return date and bool(re.match(r'^\d{1,2}\.\d{1,2}\.\d{2,5}\Z', date))

    from_date = args.from_date
    while not is_valid_date(from_date):
        from_date = input("Start time: ")
    if not is_valid_date(args.to_date):
        cli.error("Please specify a valid end time")
    if not args.output:
        cli.error("Please specify a valid output path")

    pin = ""
    import os
    if os.isatty(0):
        while not pin.strip():
            pin = getpass('PIN: ')
    else:
        pin = sys.stdin.read().strip()

    fetcher = DkbScraper(record_html=args.debug, interactive=not args.batch)

    if args.debug:
        logger = logging.getLogger("mechanize")
        logger.addHandler(logging.StreamHandler(sys.stdout))
        logger.setLevel(logging.INFO)
        # fetcher.br.set_debug_http(True)
        # fetcher.br.set_debug_responses(True)
        # fetcher.br.set_debug_redirects(True)
        level = logging.DEBUG
    else:
        level = logging.INFO

    logging.basicConfig(level=level, format='%(message)s')

    fetcher.login(args.userid, pin)
    fetcher.transactions_overview()

    c_accs, d_accs = fetcher.list_accounts()
    first = True

    for c in c_accs:
        if not first:
            fetcher.br.back()
        first = False

        fetcher.select_credit_card_transactions(c, from_date, args.to_date)
        csv_text = fetcher.get_transaction_csv()
        fname = os.path.join(args.output, fetcher.get_csv_name())
        logger.info(f"Writing {fname}")
        f = open(fname, 'w')
        f.write(csv_text)

    for d in d_accs:
        if not first:
            fetcher.br.back()
        first = False

        fetcher.select_giro_transactions(d, from_date, args.to_date)
        csv_text = fetcher.get_transaction_csv()
        fname = os.path.join(args.output, fetcher.get_csv_name())
        logger.info(f"Writing {fname}")
        f = open(fname, 'w')
        f.write(csv_text)

    fetcher.logout()

# Testing
# =======
# python -m unittest dkb
# test_fetcher will fail unless you manually create test data, see below


class TestDkb(unittest.TestCase):
    def test_fetcher(self):
        # Run with --debug to create the necessary data for the tests.
        # This will record your actual dkb.de responses for local testing.
        fetcher = DkbScraper(playback_html=True)
        # fetcher.br.set_debug_http(True)
        # fetcher.br.set_debug_responses(True)
        # fetcher.br.set_debug_redirects(True)

        logger = logging.getLogger("mechanize")
        logger.addHandler(logging.StreamHandler(sys.stdout))
        logger.setLevel(logging.INFO)

        logging.basicConfig(level=logging.INFO, format='%(message)s')

        fetcher.login("test", "1234")
        fetcher.transactions_overview()
        c_accs, d_accs = fetcher.list_accounts()

        first = True

        for c in c_accs:
            if not first:
                fetcher.br.back()
            first = False

            fetcher.select_credit_card_transactions(
                c, "01.01.2013", "01.09.2013")
            print(c)
            print(fetcher.get_transaction_csv())
            print(fetcher.get_csv_name())
            print()

        for d in d_accs:
            if not first:
                fetcher.br.back()
            first = False

            fetcher.select_giro_transactions(d, "01.01.2013", "01.09.2013")
            print(d)
            print(fetcher.get_transaction_csv())
            print(fetcher.get_csv_name())
            print()

        fetcher.logout()
