# Define here the models for your spider middleware
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/spider-middleware.html
import time
import random
from scrapy import signals, Request
from scrapy.http import HtmlResponse
from selenium import webdriver
from twisted.internet.threads import deferToThread
from twisted.internet import defer, reactor

import threading
import logging
from selenium.common import WebDriverException

# useful for handling different item types with a single interface
from itemadapter import ItemAdapter

# здесь устанавливается максимальное число запросов из одного порта
from urllib3 import poolmanager

_original_init = poolmanager.PoolManager.__init__

def _patched_init(self, *args, **kwargs):
    # по‑умолчанию maxsize = 1; увеличиваем, например, до 10
    kwargs.setdefault('maxsize', 10)
    return _original_init(self, *args, **kwargs)

poolmanager.PoolManager.__init__ = _patched_init


class WbSpiderSpiderMiddleware:
    # Not all methods need to be defined. If a method is not defined,
    # scrapy acts as if the spider middleware does not modify the
    # passed objects.

    @classmethod
    def from_crawler(cls, crawler):
        # This method is used by Scrapy to create your spiders.
        s = cls()
        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def process_spider_input(self, response, spider):
        # Called for each response that goes through the spider
        # middleware and into the spider.

        # Should return None or raise an exception.
        return None

    def process_spider_output(self, response, result, spider):
        # Called with the results returned from the Spider, after
        # it has processed the response.

        # Must return an iterable of Request, or item objects.
        for i in result:
            yield i

    def process_spider_exception(self, response, exception, spider):
        # Called when a spider or process_spider_input() method
        # (from other spider middleware) raises an exception.

        # Should return either None or an iterable of Request or item objects.
        pass

    async def process_start(self, start):
        # Called with an async iterator over the spider start() method or the
        # matching method of an earlier spider middleware.
        async for item_or_request in start:
            yield item_or_request

    def spider_opened(self, spider):
        spider.logger.info("Spider opened: %s" % spider.name)


class WbSpiderDownloaderMiddleware:
    def __init__(self, crawler):
        self.logger = logging.getLogger(__name__)
        self.settings = crawler.settings
        self._thread_local = threading.local()
        self._active_drivers = []  # Для корректного закрытия
        self._closed = False

        crawler.signals.connect(self.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(self.spider_closed, signal=signals.spider_closed)


    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)


    def spider_opened(self, spider):
        """Сигнализирует краулеру об активном статусе паука"""
        # настройка размера пула Twisted
        workers = self.settings.getint('SELENIUM_WORKERS')
        reactor.suggestThreadPoolSize(workers)
        self.logger.info(f"SeleniumMiddleware: Twisted thread pool set to {workers}")


    def spider_closed(self, spider, reason):
        """Сигнализирует краулеру о завершении работы паука"""
        self._closed = True


    def process_request(self, request: Request, spider):
        """Обрабатывает запрос scrapy.Request и передаёт методу-колбэку"""
        if not request.meta.get('selenium'):
            return None

        # безопасный перенос блокирующего кода в пул Twisted
        return deferToThread(self._fetch_with_selenium, request)


    def _fetch_with_selenium(self, request: Request):
        """Возвращает "чистый" драйвер с открытой страницей из запроса"""
        driver = self._get_driver()

        try:
            # переход на запрошенную страницу
            driver.get(request.url)

            # внедрение DOWNLOAD_DELAY
            delay = self.settings.getfloat('DOWNLOAD_DELAY')
            time.sleep(random.uniform(delay * 0.5, delay * 1.5))

            # передача драйвера методу-колбэку через meta
            request.meta['driver'] = driver

            return HtmlResponse(url=request.url, request=request)
        
        except WebDriverException as wde:
            self.logger.exception(
                f"WebDriver error while loading {request.url}: {wde}",
            )

            # ответ 502 (Bad Gateway) как признак проблемы с внешним сервисом.
            return HtmlResponse(
                url=request.url,
                status=502,
                request=request,
            )


    def _get_driver(self):
        """Инициализирует Selenium Chrome WebDriver"""
        ua = self.settings.getlist('USER_AGENTS')
        options = webdriver.ChromeOptions()
        options.page_load_strategy = 'none'
        options.add_argument("--headless=new")
        options.add_argument(f"user-agent={random.choice(ua)}")
        options.add_argument("--window-size=1920,1200")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--blink-settings=imagesEnabled=false")
        options.add_experimental_option(
            "excludeSwitches", ["enable-automation"],
        )
        options.add_argument("--disable-application-cache")
        options.add_argument("--disable-cache")
        options.add_argument("--disable-offline-load-stale-cache")
        options.add_argument("--disk-cache-size=0")
        options.add_argument("--media-cache-size=0")
        options.add_experimental_option('useAutomationExtension', False)
        options.set_capability('goog:loggingPrefs', {'performance': 'INFO'})
        options.set_capability('goog:chromeOptions', {
            'perfLoggingPrefs': {'bufferSize': 10000000}
        })

        driver = webdriver.Chrome(options=options)
        timeout = self.settings.getint('DOWNLOAD_TIMEOUT')
        driver.set_page_load_timeout(timeout)
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source":
                """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                """
            }
        )
        driver.execute_cdp_cmd("Network.enable", {})

        self._thread_local.driver = driver
        return self._thread_local.driver
