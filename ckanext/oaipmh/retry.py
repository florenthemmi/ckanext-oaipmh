'''
There should really be a separate table for retries. Now errors are used for
storing retry information. On the positive side, if there is a way to view the
objects and the associated errors then the retry will show up there. If that
is of any use to anyone.
'''

class HarvesterRetry(object):
    '''
    Class for dealing with harvest_objects that need to be retried later.
    '''

    @staticmethod
    def _retry_message(harvest_job):
        return u'retry ' + harvest_job.source_id

    @staticmethod
    def mark_for_retry(harvest_object):
        '''
        Marks a harvest object for retry later.
        '''
        pass

    def find_all_retries(self, harvest_job):
        '''
        Finds list of all retries related to the earlier harvest_jobs that
        match the given harvest_job. Returns a list of HarvestObjects.
        '''
        return []

    def clear_retry_marks(self):
        '''
        Finds all retries related to previous harvest_job and clears the marks.
        Only do this once you have successfully built a list of harvest_objects.
        '''
        pass
