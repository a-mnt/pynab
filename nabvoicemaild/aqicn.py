# -*- coding: utf-8 -*-
"""
voicemail
"""

import requests
import json
import logging

class voicemailError(Exception):
    """Raise when errors occur while fetching or parsing data"""


class voicemailClient:
    """Client to fetch and parse data from voicemail"""

    def __init__(self, indice, update=False):
        """Initialize the client object."""
        self._message = 0
        self._horodatage = "-"
        if update == True:
            self.update()

    def update(self):
        """Fetch new data and format it"""
        

    def _fetch_airquality_data(self):

        try:
           return self._message 



        except Exception as err:
            raise voicemailError(err)

    def get_message(self):
        return self._message

    def get_horodatage(self):
        return self._horodatage
