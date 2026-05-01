
<p align="center">
	<img src="https://raw.githubusercontent.com/91JJ/HomeAssisstant_HACS_Utilita_Energy_Beta/main/custom_components/utilita/logo/utilita_logo.png" alt="Utilita logo" width="500">
</p>

# Utilita Energy Integration for Home Assistant

This integration allows Home Assistant to fetch energy usage, balance, tariff, and payment data from Utilita Energy accounts.  
A lot of the data is stored within the Attributes so may require other integrations to utilise the data if required.  

## UPDATE - v2.1 Handling Multi-factor Authentication (MFA)
Utilita recently enforced MFA which broke the connection on this integration.

~~This has now been amended to automatically select the email option to send the MFA code.~~
You now have the option to select email or mobile.

During setup, after entering the correct E-Mail / Password, you will be requested to select email or mobile for a OTP (One-Time Password).

After selecting, a box will appear requesting the 6 digit code sent via email.
After entering the OTP code, the integration should be created :smiley:.

## Installation
### HACS
1. Open HACS.
2. Go to **Integrations**.
3. Select the three-dot menu, then **Custom repositories**.
4. Add `https://github.com/91JJ/HomeAssisstant_HACS_Utilita_Energy_Beta` as an **Integration** repository.
5. Search for **Utilita Energy** in HACS and install it.
6. Restart Home Assistant.
7. Go to **Settings > Devices & Services > Add Integration** and search for **Utilita Energy**.
8. Enter your Utilita email, password, refresh rate, MFA method, and OTP code.

### Manual
1. Copy the `custom_components/utilita` folder from this repository into your Home Assistant `custom_components/` directory.
2. Restart Home Assistant.
3. Go to **Settings > Devices & Services > Add Integration** and search for **Utilita Energy**.
4. Enter your Utilita email, password, refresh rate, MFA method, and OTP code.


## Configuration
- **Email**: Your Utilita account email.
- **Password**: Your Utilita account password.
- **Refresh Rate**: Data polling interval in seconds (minimum 300).
- **OTP**: MFA code sent by your selected MFA method.


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
