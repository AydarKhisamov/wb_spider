import os
import json
import time
import random

from scrapy import Spider, Request
from scrapy.exceptions import CloseSpider
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
)

from wb_spider.settings import DOWNLOAD_TIMEOUT, RETRY_TIMES, RETRY_DELAY

class WBSpider(Spider):
    """Пользовательский подкласс паука Scrapy."""
    name = "wb_spider"
    search_query = "пальто из натуральной шерсти"

    # папка для сохранения страниц с ошибками
    snapshot_path = "errors"
    if snapshot_path not in os.listdir():
        os.mkdir(snapshot_path)

    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


    def start_requests(self):
        """Генерирует ссылку на каталог товаров по поисковому запросу"""
        start_urls = [
            ''.join([
                'https://www.wildberries.ru/catalog/0/search.aspx?search=',
                '%20'.join(self.search_query.split(' ')),
            ])
        ]

        try:
            for url in start_urls:
                yield Request(
                    url,
                    callback=self.parse_catalogue,
                    meta={"selenium": True}
                )

        except Exception as e:
            self._handle_fatal_error(e, "Start of parsing")


    def parse_catalogue(self, response):
        """Возвращает ссылки на товары из каталога"""
        try:
            # драйвер из middleware
            driver = response.meta['driver']

            # селекторы каталога и товаров
            catalogue_selector = "div[class='catalog-page__main']"
            item_selector = (
                "a[class='product-card__link j-card-link j-open-full-product-card']"
            )

            # ожидание появления каталога
            try:
                catalogue = WebDriverWait(driver, DOWNLOAD_TIMEOUT).until(
                    EC.presence_of_element_located((
                        By.CSS_SELECTOR, catalogue_selector,
                    ))
                )

            except TimeoutException:
                pass

            if catalogue:
                # Счётчик скроллинга
                scroll_counter = 0

                # скроллинг каталога, пока не появятся новые товары
                #while True:
                for _ in range(25):
                    # кол-во товаров до скроллинга
                    initial_count = len(
                        driver.find_elements(By.CSS_SELECTOR, item_selector)
                    )

                    # скроллинг до конца каталога
                    driver.execute_script(
                        """
                        const catalogue = arguments[0];
                        const rect = catalogue.getBoundingClientRect();
                        const scrollPosition = window.pageYOffset + rect.bottom - window.innerHeight;
                        window.scrollTo({
                            top: Math.max(0, scrollPosition),
                            behavior: 'smooth'
                        });
                        """,
                        catalogue,
                    )

                    # проверка появления новых элементов
                    try:
                        WebDriverWait(driver, 60).until(
                            lambda driver: len(
                                driver.find_elements(
                                    By.CSS_SELECTOR, item_selector,
                                )
                            ) > initial_count
                        )

                        scroll_counter += 1

                    except TimeoutException:
                        self.logger.info(
                            "Scrolling has been completed "
                            + f"after {scroll_counter} attempts"
                        )
                        break
            
            # поиск карточек товаров
            item_cards = driver.find_elements(
                By.CSS_SELECTOR, item_selector,
            )
            self.logger.info(f"{len(item_cards)} items has been found")

            # отбор ссылок на товары
            item_urls = [card.get_attribute("href") for card in item_cards]

            # закрытие драйвера
            driver.quit()

            for url in item_urls:
                yield Request(
                    url,
                    callback=self.parse_item,
                    meta={"selenium": True},
                )
                
        except Exception as e:
            self._handle_fatal_error(e, "Parsing of catalogue", driver)

            
    def parse_item(self, response):
        """Возвращает данные о товаре"""
        try:
            # драйвер из middleware
            driver = response.meta['driver']

            # словарь с данными о товаре
            item = {}

            # флаги успешного парсинга данных из ответов на сетевые запросы
            parsed_flag1 = False
            parsed_flag2 = False

            # ожидание загрузки таблицы с характеристиками товаров
            # к этому времени будет получен ответ на все сетевые запросы
            try:
                WebDriverWait(driver, DOWNLOAD_TIMEOUT).until(
                    EC.presence_of_element_located((
                        By.CSS_SELECTOR,
                        "table",
                    ))
                )

            except TimeoutException:
                pass

            item['Ссылка на товар'] = response.url
            nm_id = response.url.split('/')[-2]
            item['Артикул']  = nm_id

            for _ in range(RETRY_TIMES):
                # все сохранённые логи драйвера
                logs = driver.get_log('performance')
                for log in logs:
                    message = json.loads(log["message"])["message"]
                    method = message.get("method")   
            
                    # получение id запроса
                    if method == "Network.requestWillBeSent":
                        params = message["params"]

                        if (
                            params['request']['url'].endswith(
                                f'{nm_id}/info/ru/card.json'
                            )
                            and not parsed_flag1
                        ):
                            try:
                                # получение тела ответа на запрос
                                response = driver.execute_cdp_cmd(
                                    "Network.getResponseBody", 
                                    {"requestId": params["requestId"]}
                                )

                                # извлечение данных из ответа
                                data = json.loads(response['body'])

                                item['Название'] = data.get('imt_name', '')
                                item['Описание'] = data.get('description', '')

                                try:
                                    item['Характеристики'] = '\n'.join([
                                        f"{d['name']}: {d['value']}"
                                        for d in data['options']
                                    ])
                                except:
                                    item['Характеристики'] = ''

                                parsed_flag1 = True

                            except:
                                item['Название'] = ''
                                item['Описание'] = ''
                                item['Характеристики'] = ''


                        elif (
                            params['request']['url'].endswith(f'nm={nm_id}')
                            and not parsed_flag2
                        ):
                            try:
                                # получение тела ответа на запрос
                                response = driver.execute_cdp_cmd(
                                    "Network.getResponseBody", 
                                    {"requestId": params["requestId"]}
                                )

                                # извлечение данных из ответа
                                data = json.loads(response['body'])['products'][0]

                                item['Название селлера'] = data.get('supplier', '')
                                item['Ссылка на селлера'] = ''
                                seller_id = data.get('supplierId')
                                if seller_id:
                                    item['Ссылка на селлера'] = (
                                        "https://www.wildberries.ru/seller/"
                                        + str(seller_id)
                                    )

                                item['Рейтинг'] = data.get('reviewRating', '')
                                item['Количество отзывов'] = data.get('nmFeedbacks', '')

                                item['Цена'] = ''
                                try:
                                    sizes = []
                                    stocks = []
                                    for size_data in data['sizes']:
                                        if not item['Цена']:
                                            try:
                                                raw_price = (
                                                    size_data['price']['product']
                                                )

                                                # разделение разрядов пробелами
                                                # добавление символа валюты
                                                item['Цена'] = (
                                                    f'{raw_price // 100:_} ₽'
                                                    .replace('_', ' ')
                                                )
                                            except KeyError:
                                                pass

                                        sizes.append(size_data.get('name', ''))
                                        try:
                                            stock = size_data['stocks'][0]['qty']
                                            stocks.append(str(stock))
                                        except IndexError:
                                            stocks.append('0')
                                        except:
                                            stocks.append('-')
                                    
                                    item['Размер'] = ','.join(sizes)
                                    item['Остатки'] = ','.join(stocks)

                                except:
                                    item['Размер'] = ''
                                    item['Остатки'] = ''

                                parsed_flag2 = True

                            except:
                                item['Название селлера'] = ''
                                item['Ссылка на селлера'] = ''
                                item['Рейтинг'] = ''
                                item['Количество отзывов'] = ''
                                item['Цена'] = ''
                                item['Размер'] = ''
                                item['Остатки'] = ''

                    if all([parsed_flag1, parsed_flag2]):
                        break
            
                if all([parsed_flag1, parsed_flag2]):
                    break

                # если спарсились не все данные,
                # то перезагрузка страницы с задержкой
                time.sleep(
                    random.uniform(0.5 * RETRY_DELAY, 1.5 * RETRY_DELAY)
                )
                driver.refresh()

            try:
                # извлечение ссылок на изображения
                img_selector = (
                    f"img[src*='{nm_id}/images/big'], "
                    + f"img[src*='{nm_id}/images/hq']"
                )
                img_els = driver.find_elements(By.CSS_SELECTOR, img_selector)
                img_urls = [el.get_attribute('src') for el in img_els]
                item['Ссылки на изображения'] = ','.join(list(set(img_urls)))

            except NoSuchElementException:
                item['Ссылки на изображения'] = ''

            # закрытие драйвера
            driver.quit()
            yield item
            
        except Exception as e:
            self._handle_fatal_error(e, "Product parsing", driver)


    def _handle_fatal_error(self, error, context, driver=None):
        """Сохраняет HTML и завершает работу при критической ошибке"""
        error_msg = f"FATAL ERROR in {context}: {str(error)}"
        self.logger.error(error_msg)

        if driver:
            # сохранение текущего HTML для диагностики
            self._create_snapshot(driver, 'fatal_error_snapshot.html')
            
            # закрытие драйвера
            driver.quit()

        raise CloseSpider


    def _create_snapshot(self, driver, fname):
        """Сохраняет текущую страницу"""
        with open(
            f"{self.snapshot_path}/{fname}", "w", encoding="utf-8",
        ) as f:
            f.write(driver.page_source)

        self.logger.info(f"Page HTML saved to {self.snapshot_path}/{fname}")
