from onadata.apps.restservice.models import RestService


def call_service(submission_instance):
    # lookup service
    services = RestService.objects.exclude(
        xform_id=submission_instance.xform_id, name='googlesheet'
    )
    # call service send with url and data parameters
    for sv in services:
        # TODO: Queue service
        try:
            service = sv.get_service_definition()()
            service.send(sv.service_url, submission_instance)
        except Exception:
            # TODO: Handle gracefully | requeue/resend
            pass
