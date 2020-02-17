import dateutil.parser
import os
import sys
import logging
import re

from echo360.hls_downloader import Downloader
from echo360.exceptions import EchoLoginError

from pick import pick
from selenium import webdriver
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
import selenium.common.exceptions as seleniumException
import warnings  # hide the warnings of phantomjs being deprecated
warnings.filterwarnings("ignore", category=UserWarning, module='selenium')

import subprocess

_LOGGER = logging.getLogger(__name__)


class EchoDownloader(object):
    def __init__(self,
                 course,
                 output_dir,
                 date_range,
                 username,
                 password,
                 setup_credential,
                 use_local_binary=False,
                 use_chrome=False,
                 interactive_mode=False):
        self._course = course
        root_path = os.path.dirname(
            os.path.abspath(sys.modules['__main__'].__file__))
        if output_dir == '':
            output_dir = root_path
        self._output_dir = output_dir
        self._date_range = date_range
        self._username = username
        self._password = password
        self.interactive_mode = interactive_mode

        self.regex_replace_invalid = re.compile(r'[\\\\/:*?\"<>|]')

        # define a log path for phantomjs to output, to prevent hanging due to PIPE being full
        log_path = os.path.join(root_path, 'webdriver_service.log')

        self._useragent = "Mozilla/5.0 (iPad; CPU OS 6_0 like Mac OS X) AppleWebKit/536.26 (KHTML, like Gecko) Version/6.0 Mobile/10A5376e Safari/8536.25"
        # self._driver = webdriver.PhantomJS()

        dcap = dict(DesiredCapabilities.PHANTOMJS)
        dcap["phantomjs.page.settings.userAgent"] = (
            "Mozilla/5.0 (iPad; CPU OS 6_0 like Mac OS X) AppleWebKit/536.26 "
            "(KHTML, like Gecko) Version/6.0 Mobile/10A5376e Safari/8536.25")
        if use_local_binary:
            if use_chrome:
                from echo360.binary_downloader.chromedriver import ChromedriverDownloader
                get_bin = ChromedriverDownloader().get_bin
            else:
                from echo360.binary_downloader.phantomjs import PhantomjsDownloader
                get_bin = PhantomjsDownloader().get_bin
            kwargs = {
                'executable_path': get_bin(),
                'desired_capabilities': dcap,
                'service_log_path': log_path
            }
        else:
            kwargs = {}
        if use_chrome:
            from selenium.webdriver.chrome.options import Options
            opts = Options()
            if not setup_credential:
                opts.add_argument("--headless")
            opts.add_argument("--window-size=1920x1080")
            opts.add_argument("user-agent={}".format(self._useragent))
            kwargs['chrome_options'] = opts
            self._driver = webdriver.Chrome(**kwargs)
        else:
            self._driver = webdriver.PhantomJS(**kwargs)

        # Monkey Patch, set the course's driver to the one from downloader
        self._course.set_driver(self._driver)
        self._videos = []

    def login(self):
        # Initialize to establish the 'anon' cookie that Echo360 sends.
        self._driver.get(self._course.url)
        # First see if we have successfully access course page without the need to login
        # for example: https://view.streaming.sydney.edu.au:8443/ess/portal/section/ed9b26eb-a785-4f4e-bd51-69f3faab388a
        if self.find_element_by_partial_id('username') is not None:
            self.loginWithCredentials()
        else:
            # check if it is network error
            if '<html><head></head><body></body></html>' in self._driver.page_source:
                print('Failed!')
                print(
                    '  > Failed to connect to server, is your internet working...?'
                )
                _LOGGER.debug("Network seems to be down")
                _LOGGER.debug("Dumping page at %s: %s", self._course.url,
                              self._driver.page_source)
                raise EchoLoginError(self._driver)
            elif 'check your URL' in self._driver.page_source:
                print('Failed!')
                print(
                    '  > Failed to connet to course page, is the uuid correct...?'
                )
                _LOGGER.debug("Failed to find a valid course page")
                _LOGGER.debug("Dumping page at %s: %s", self._course.url,
                              self._driver.page_source)
                raise EchoLoginError(self._driver)
            else:
                # Should be only for the case where login details is not required left
                print('INFO: No need to login :)')
                _LOGGER.debug("No username found (no need to login?)")
                _LOGGER.debug("Dumping login page at %s: %s", self._course.url,
                              self._driver.page_source)
        self.retrieve_real_uuid()
        print('Done!')

    def loginWithCredentials(self):
        _LOGGER.debug("Logging in with credentials")
        # retrieve username / password if not given before
        if self._username is None or self._password is None:
            print('Credentials needed...')
            if self._username is None:
                if sys.version_info < (3, 0):  # special handling for python2
                    input = raw_input
                else:
                    from builtins import input
                self._username = input('Unikey: ')
            if self._password is None:
                import getpass
                self._password = getpass.getpass('Passowrd for {0}: '.format(
                    self._username))
        # Input username and password:
        # user_name = self._driver.find_element_by_id('j_username')
        user_name = self.find_element_by_partial_id('username')
        user_name.clear()
        user_name.send_keys(self._username)

        # user_passwd = self._driver.find_element_by_id('j_password')
        user_passwd = self.find_element_by_partial_id('password')
        user_passwd.clear()
        user_passwd.send_keys(self._password)

        try:
            login_btn = self._driver.find_element_by_id('login-btn')
            login_btn.submit()
        except seleniumException.NoSuchElementException:
            # try submit via enter key
            from selenium.webdriver.common.keys import Keys
            user_passwd.send_keys(Keys.RETURN)

        # test if the login is success
        if self.find_element_by_partial_id('username') is not None:
            print('Failed!')
            print('  > Failed to login, is your username/password correct...?')
            raise EchoLoginError(self._driver)

    def download_all(self):
        sys.stdout.write('>> Logging into "{0}"... '.format(self._course.url))
        sys.stdout.flush()
        self.login()
        sys.stdout.write('>> Retrieving echo360 Course Info... ')
        sys.stdout.flush()
        videos = self._course.get_videos().videos
        print('Done!')
        # change the output directory to be inside a folder named after the course
        self._output_dir = os.path.join(self._output_dir, u'{0} - {1}'.format(
            self._course.course_id, self._course.course_name).strip())
        # replace invalid character for folder
        self.regex_replace_invalid.sub('_', self._output_dir)

        filtered_videos = [
            video for video in videos if self._in_date_range(video.date)
        ]
        videos_to_be_download = []
        for video in reversed(filtered_videos):  # reverse so we download newest first
            lecture_number = self._find_pos(videos, video)
            title = "Lecture {} [{}]".format(lecture_number + 1, video.title)
            filename = self._get_filename(self._course.course_id, video.date,
                                          title)
            videos_to_be_download.append((filename, video))
        if self.interactive_mode:
            title = "Select video(s) to be downloaded (SPACE to mark, ENTER to continue):"
            selected = pick([v[0] for v in videos_to_be_download], title,
                            multi_select=True, min_selection_count=1)
            videos_to_be_download = [videos_to_be_download[s[1]] for s in selected]

        print(u'=' * 60)
        print(u'    Course: {0} - {1}'.format(self._course.course_id,
                                             self._course.course_name))
        print(u'      Total videos to download: {0} out of {1}'.format(
            len(videos_to_be_download), len(videos)))
        print(u'=' * 60)

        downloaded_videos = []
        for filename, video in videos_to_be_download:
            playpath = video.url.split("_definst_/")[1]
            video.dlprocess = subprocess.Popen(["rtmpdump", "-R", "-r", video.url, "-y", playpath, "-o", "default_out_path/" + filename + ".flv"])

        for filename, video in videos_to_be_downloaded:
            video.dlprocess.wait()
            downloaded_videos.insert(0, filename)
        print(self.success_msg(self._course.course_name, downloaded_videos))
        self._driver.close()

    @property
    def useragent(self):
        return self._useragent

    @useragent.setter
    def useragent(self, useragent):
        self._useragent = useragent

    def _download_as(self, video, filename):
        print('')
        print('-' * 60)
        print('Downloading "{}"'.format(filename))
        echo360_downloader = Downloader(50)
        echo360_downloader.run(video, self._output_dir)

        # rename file
        ext = echo360_downloader.result_file_name
        ext = ext[ext.rfind('.') + 1:]
        os.rename(
            os.path.join(echo360_downloader.result_file_name),
            os.path.join(self._output_dir, '{0}.{1}'.format(filename, ext)))
        print('-' * 60)

    def _initialize(self, echo_course):
        self._driver.get(self._course.url)

    def _get_filename(self, course, date, title):
        filename = "{} - {} - {}".format(course, date, title)
        # replace invalid character for files
        return self.regex_replace_invalid.sub('_', filename)

    def _in_date_range(self, date_string):
        the_date = dateutil.parser.parse(date_string).date()
        return self._date_range[0] <= the_date and the_date <= self._date_range[1]

    def _find_pos(self, videos, the_video):
        for i, video in enumerate(videos):
            if video == the_video:  # compare by object id, because date could possibly be the same in some case.
                return i

    def success_msg(self, course_name, videos):
        bar = u'=' * 65
        msg = u'\n{0}\n'.format(bar)
        msg += u'    Course: {0} - {1}'.format(self._course.course_id,
                                              self._course.course_name)
        msg += u'\n{0}\n'.format(bar)
        msg += u'    Successfully downloaded:\n'
        for i in videos:
            msg += u'        {}\n'.format(i)
        msg += u'{0}\n'.format(bar)
        return msg

    def find_element_by_partial_id(self, id):
        try:
            return self._driver.find_element_by_xpath(
                "//*[contains(@id,'{0}')]".format(id))
        except seleniumException.NoSuchElementException:
            return None

    def retrieve_real_uuid(self):
        # patch for cavas (canvas.sydney.edu.au) where uuid is hidden in page source
        # we detect it by trying to retrieve the real uuid
        uuid = re.search(
            '/ess/client/section/([0-9a-zA-Z]{8}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{12})',
            self._driver.page_source)
        if uuid is not None:
            uuid = uuid.groups()[0]
            self._course._uuid = uuid
