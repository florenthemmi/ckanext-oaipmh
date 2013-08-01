class GatherFailure(Exception):
    def __init__(self, message='', ids=[]):
        # ids is used to pass objects for retry. They could be harvested even
        # if the server did not return proper info for new items.
        self.message = message
        self.harvest_obj_ids = ids
