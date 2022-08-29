import configparser
import json
import os
from datetime import datetime, timedelta
from email.policy import strict

import pandas as pd
import pytz
import requests
from bs4 import BeautifulSoup
from slack_sdk import WebClient

webhook = os.environ['WEBHOOK']

def convert_to_readable_date(date):
    """
    Convert a date like '2022-08-29T18:03:38.483Z' to the format YYYY-MM-DD %I:%M %p in central time
    """
    date = datetime.datetime.strptime(date, '%Y-%m-%dT%H:%M:%S.%fZ')
    date = date.replace(tzinfo=pytz.utc)
    date = date.astimezone(pytz.timezone('US/Central'))
    return date.strftime('%Y-%m-%d %I:%M %p')

try:
    # Set up whether we are on dev
    is_dev = False
    # Parse the config
    # cp = configparser.ConfigParser(interpolation=None)
    # # read env file based on location
    # if os.path.exists('../../.env'):
    #     cp.read('../../.env')
    # elif os.path.exists('/Users/yzhao/.env'):  # is dev
    #     cp.read('/Users/yzhao/.env')
    #     is_dev = False
    # else:  # production
    #     cp.read("/home/ec2-user/Projects/deploy-engine/.env")

    # # Prep slack
    # app_slack_token = cp.get('slack', 'slack_bot_token')
    # # The app token grants greater permissions
    # app_sc = WebClient(app_slack_token)
    # Get timezones
    tz = pytz.timezone('US/Central')
    utc_tz = pytz.timezone('UTC')
    # Get UTC date for API calls
    # Need to subtract an hour because the current data might not be ready yet
    # And the API calls throttle quickly
    now_minus_hours = (datetime.now(utc_tz) -
                       timedelta(hours=12)).strftime('%s')
    # NOTE: This was the quake tracker channel for a second, PF changed it to fire
    tx_fire_channel = "C0409RYE1PS"

    # read fire file based on location
    if os.path.exists('/home/ec2-user/Projects/deploy-engine/cronjobs/tx_fire-feed/'):
        file_prefix = '/home/ec2-user/Projects/deploy-engine/cronjobs/tx_fire-feed/'
    else:
        file_prefix = ''

    fileold = file_prefix+'tx_fire_data.json'
    fileold_archive = file_prefix+'tx_fire_data_archive.json'
    updatekey = 'Updated'

    # GET tfs DATA
    url1 = 'https://public.tfswildfires.com/api/incidents'
    responseTx = requests.get(url1, verify=False)

    # GET INCIWEB DATA
    # We are swapping to use their Esri json feed
    url2 = 'https://inciweb.nwcg.gov/feeds/json/esri/'
    responseInci = requests.get(url2, verify=False)

    # exit early if it's a bad status code
    if responseTx.status_code != 200 or responseInci.status_code != 200:

        # check Slack history to make sure we only alert every 12 hours
        response = app_sc.conversations_history(
            channel=tx_fire_channel,
            oldest=now_minus_hours
        )

        already_alerted = False
        for message in response['messages']:
            if ("Feeds are temporarily down" in message['text']):
                already_alerted = True

        if not already_alerted:
            message = {"text": "*Feeds are temporarily down!*\TFS URL: "+url1+"\nStatus code: " + str(
                responseTx.status_code)+"\nInciWeb URL: "+url2+"\nStatus code: " + str(responseInci.status_code)}
            r = requests.post(
                webhook, data=json.dumps(message),
                headers={'Content-Type': 'application/json'}
            )

        # exit early
        exit()

    # Start list using CalFire
    newData = json.loads(responseTx.text, strict=False)['features']

    # Slot the agency into this data
    for fire in newData:
        fire['properties']['admindivision'] += " County"
        fire['properties']['Agency'] = "TEXAS A&M FOREST SERVICE"
        fire['properties']['Url'] = "https://public.tfswildfires.com/"
        fire['properties']['Name'] = fire['properties']['name']
        fire['properties']['Updated'] = convert_to_readable_date(fire['properties']['lastupdated'])
        fire['properties']['County'] = fire['properties']['admindivision']
        fire['properties']['AcresBurned'] = fire['properties']['size']
        fire['properties']['PercentContained'] = fire['properties']['containment']

    # Add to list from inciweb
    inciData = json.loads(responseInci.text, strict=False)

    for incident in inciData['markers']:
        if (incident['type'].lower() == "wildfire" and incident['state'].lower() == "texas"):
            # We have a valid fire, record the data
            newDict = {
                "properties": {"Name": incident['name'],
                               "Url": 'https://inciweb.nwcg.gov' + incident['url'],
                               "Updated": incident['updated'],
                               "PercentContained": str(incident['contained']) + "%",
                               "AcresBurned": incident['size'],
                               "County": "https://www.google.com/maps/@{},{},12z".format(incident['lat'], incident['lng']),
                               "Agency": "InciWeb"}
            }
            # Add data
            newData.append(newDict)

    # Open old data file from system
    reset = False
    try:
        with open(fileold) as jsonfile:
            olddict = json.load(jsonfile, strict=False)

        # save a copy of this old data for debugging purposes
        with open(fileold_archive, 'w') as f:
            json.dump(olddict, f, ensure_ascii=False)

    except Exception as e:
        # We couldn't open the JSON file, which means it got deleted somehow or isn't valid JSON -- that's fine, we'll regenerate it, but don't alert
        reset = True
        olddict = {}

    # compare nested dicts
    alertstring = "*{agency}: {fire_name}*\nLocation: {fire_county}\nAcres burned: {acres_burned}\nContainment: {containment}\nLast updated: {fire_updated_date}\nLink: {fire_link}"
    for fire in newData:
        alert = ''
        important_updates = False

        # check if there's a matching old fire
        oldfire = None
        for x in olddict:
            # We had the same fire come up from multiple agencies
            # patching like this to squash slack alerts till we decide how to handle
            # -PF

            # Now we have same fire names from the SAME agency
            # We are breaking them up by start time
            # - EW

            # But it turns out, the start time will vary randomly on time! But it shouldn't vary on date
            # So splitting out the date and ignoring the start -PF

            if x['properties']['Name'] == fire['properties']['Name'] and x['properties']['Agency'] == fire['properties']['Agency']:
                # Match up start times too
                if 'firsttimestatus' in x and 'firsttimestatus' in fire:
                    date_string_new = fire['properties']['firsttimestatus'].split('T')[
                        0]
                    date_string_old = x['properties']['firsttimestatus'].split('T')[
                        0]

                    if date_string_old == date_string_new:
                        oldfire = x
                else:
                    # This is InciWeb and we don't have start time
                    oldfire = x
                    break
        # if not, it's new!
        if not oldfire:
            important_updates = True
            alert = ":bangbang: *NEW* "
            try:
                alert += alertstring.format(
                    agency=fire['properties']['Agency'],
                    fire_name=fire['properties']['Name'],
                    fire_county=fire['properties']['County'],
                    fire_updated_date=fire['properties']['Updated'],
                    acres_burned=fire['properties']['AcresBurned'],
                    containment=fire['properties']['PercentContained'],
                    fire_link='https://public.tfswildfires.com/'
                )
            except Exception as e:
                message = {"text": "*PYTHON ERROR at new fire*:\n" + str(e)}
                r = requests.post(
                    webhook, data=json.dumps(message),
                    headers={'Content-Type': 'application/json'}
                )
        # or if there's update for existing fire
        elif oldfire['properties'][updatekey] != fire['properties'][updatekey]:
            # print("ALERTING OLDFIRE")
            # print(oldfire[updatekey])
            # print(fire[updatekey])
            try:
                alert = alertstring.format(
                    agency=fire['properties']['Agency'],
                    fire_name=fire['properties']['Name'],
                    fire_county=fire['properties']['County'],
                    fire_updated_date=fire['properties']['Updated'],
                    acres_burned=fire['properties']['AcresBurned'],
                    containment=fire['properties']['PercentContained'],
                    fire_link=fire['properties']['Url']
                )
            except Exception as e:
                message = {
                    "text": "*PYTHON ERROR at found updated fire*:\n" + str(e)}
                r = requests.post(
                    webhook, data=json.dumps(message),
                    headers={'Content-Type': 'application/json'}
                )
            # loop through information to see what changed beside last update date

            for nestedkey in fire['properties']:
                try:
                    if nestedkey != updatekey and fire['properties'][nestedkey] != oldfire['properties'][nestedkey]:

                        if nestedkey == 'firsttimestatus':
                            # if it's the start date, only alert if the actual date has changed
                            # since there's bugginess with the time

                            date_string_new = fire[nestedkey].split('T')[0]
                            date_string_old = oldfire[nestedkey].split('T')[0]

                            if date_string_new != date_string_old:
                                important_updates = True
                        else:
                            # otherwise, please alert
                            important_updates = True

                        alert += '\n:rotating_light: *Updated* {}: {}'.format(
                            nestedkey, fire['properties'][nestedkey])

                except Exception as e:
                    message = {
                        "text": "*PYTHON ERROR at enumerating updates*:\n" + str(e)}
                    r = requests.post(
                        webhook, data=json.dumps(message),
                        headers={'Content-Type': 'application/json'}
                    )
        # send alert if there's new fire info (and it's not a complete reset)
        if alert and important_updates and not reset:
            message = {"text": alert}
            if is_dev:
                print(message)
            else:
                r = requests.post(
                    webhook, data=json.dumps(message),
                    headers={'Content-Type': 'application/json'}
                )

    # write new json
    with open(fileold, 'w') as f:
        json.dump(newData, f, ensure_ascii=False)


except Exception as e:
    message = {"text": "*PYTHON ERROR!*:\n" + str(e)}

    if is_dev:
        print(message)
    else:
        r = requests.post(
            webhook, data=json.dumps(message),
            headers={'Content-Type': 'application/json'}
        )
