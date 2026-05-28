
<p align="center">
	<img src="custom_components/utilita/logo/utilita_logo.png" alt="Utilita logo" width="500">
</p>

# Utilita Energy Integration for Home Assistant

This integration allows Home Assistant to fetch energy usage, balance, tariff, and payment data from Utilita Energy accounts.  
A lot of the data is stored within the Attributes so may require other integrations to utilise the data if required.  

## Changelog — v3.0.0

- **Device card fix** — Sensors are now correctly grouped under a single **Utilita Energy** device card in **Settings → Devices & Services**. Previously, a race condition caused an entity-less device to be created before sensors were registered, which Home Assistant 2025.x filtered from the UI.
- **Cookie-based authentication** — The login flow has been completely removed. No email, password, MFA, OTP, or Cloudflare cookies are needed. You log into the Utilita website normally in your browser and paste the resulting session cookies into Home Assistant. This approach is simpler, more reliable, and immune to Cloudflare changes. See the **[Setup section below](#setup--getting-your-session-cookies)** for instructions.
- **MFA / OTP support removed** — No longer needed since the integration no longer performs a login at all.
- **Cloudflare `cf_clearance` workaround removed** — Superseded by the cookie-based approach.

## Setup — Getting Your Session Cookies

This integration works by re-using the session cookies that your browser receives when you log into the Utilita website. No credentials are stored in Home Assistant.

### Steps

1. Open your browser and log into **[https://my.utilita.co.uk](https://my.utilita.co.uk)**.
2. Press **F12** to open DevTools.
3. Go to the **Network** tab.
4. Reload the page (**[https://my.utilita.co.uk](https://my.utilita.co.uk)**.).
5. Click on any request to `my.utilita.co.uk` in the list.
6. In the right-hand panel, scroll to **Request Headers**.
7. Find the **Cookie** header and copy its **entire value** (it will look like `XSRF-TOKEN=abc...; laravel_session=xyz...; ...`).
8. Paste this value into the **Session Cookies** field when setting up the integration in Home Assistant.

> **Firefox**: The steps are the same. DevTools (F12) → Network tab → reload → click a request → Request Headers → Cookie.

### How long do the cookies last?

Utilita session cookies typically last **several weeks to months** (they are set with `remember=on`). When they expire:
- Home Assistant will show a **persistent notification** in your dashboard.
- The integration will prompt you to **re-authenticate** via **Settings → Devices & Services → Utilita → Re-authenticate**.
- Simply log into the website again, copy fresh cookies, and paste them in.

## Installation
### HACS
1. Open HACS.
2. Go to **Integrations**.
3. Select the three-dot menu, then **Custom repositories**.
4. Add `https://github.com/91JJ/HomeAssisstant_HACS_Utilita_Energy_Beta` as an **Integration** repository.
5. Search for **Utilita Energy** in HACS and install it.
6. Restart Home Assistant.
7. Go to **Settings > Devices & Services > Add Integration** and search for **Utilita Energy**.
8. Follow the [setup steps above](#setup--getting-your-session-cookies) to paste in your session cookies.

### Manual
1. Copy the `custom_components/utilita` folder from this repository into your Home Assistant `custom_components/` directory.
2. Restart Home Assistant.
3. Go to **Settings > Devices & Services > Add Integration** and search for **Utilita Energy**.
4. Follow the [setup steps above](#setup--getting-your-session-cookies) to paste in your session cookies.


## Configuration
- **Session Cookies**: Required — see the [setup steps above](#setup--getting-your-session-cookies).
- **Refresh Rate**: Data polling interval in seconds (minimum 300).


## Sensors
### Sensors
- Daily Electricity Usage (_This has been noted to be days behind due to source data_)
- Daily Gas Usage (_This has been noted to be days behind due to source data_)
- Electricity Balance
- Gas Balance
- Monthly Electricity Usage
- Monthly Gas Usage
- Weekly Electricity Usage
- Weekly Gas Usage
- Yearly Electricity Usage
- Yearly Gas Usage

### Diagnostic
- Account
- Current Electric Rate (_This has been noted to be days behind due to source data_)
- Current Gas Rate (_This has been noted to be days behind due to source data_)
- Electricity Tariff
- Gas Tariff
- Unread Messages

<br/>

## To-Do
- [x] Open Beta. :tada:
- [X] Create icon & publish to Brands. (Completed :tada: - https://github.com/home-assistant/brands/pull/7248#pullrequestreview-2967758252)
- [X] Add support for Multi-factor Authentication (MFA). (Completed :tada:)
- [X] Handle Cloudflare Turnstile via cookie-based authentication. (Completed :tada: v2.4.0)
- [ ] Add Service Call to update sensors on request.
- [ ] Add to Home Assistant Integrations.
- [x] Fix deprecated options flow config entry assignment.


## Home Assistant Feedback
Please leave any feedback, comments etc on Home Assistant Community...  
https://community.home-assistant.io/t/utilita-energy-uk-utility-sensors/901143

## Repository Structure
This repository now follows the standard Home Assistant custom integration layout:

- `custom_components/utilita/` contains the integration files.
- `hacs.json` is provided at the repository root for HACS.

<br/> <br/>
> [!WARNING]
>  This integration has only been tested with Pay As You Go customers.

> [!CAUTION]
>  This integration isn't supported by Utilita Energy and the APIs could change at any time causing the sensors, or worse, the integration to fail.
