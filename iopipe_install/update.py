import boto3
import collections
import itertools
import json
import shutil

from . import layers
from .combine_dict import combine_dict

IOPIPE_ARN_PREFIX_TEMPLATE = "arn:aws:lambda:%s:146318645305"
RUNTIME_CONFIG = {
    'nodejs': {
        'Handler': 'node_modules/@iopipe/iopipe/handler'
    },
    'nodejs4.3': {
        'Handler': 'node_modules/@iopipe/iopipe/handler'
    },
    'nodejs6.10': {
        'Handler': 'node_modules/@iopipe/iopipe/handler'
    },
    'nodejs8.10': {
        'Handler': 'node_modules/@iopipe/iopipe/handler'
    },
    'java8': {
        'Handler': {
            'request': 'com.iopipe.generic.GenericAWSRequestHandler',
            'stream': 'com.iopipe.generic.GenericAWSRequestStreamHandler'
        }
    },
    'python2.7': {
        'Handler': 'iopipe.handler.wrapper'
    },
    'python3.0.6': {
        'Handler': 'iopipe.handler.wrapper'
    },
    'python3.7': {
        'Handler': 'iopipe.handler.wrapper',
    }
}


def get_arn_prefix():
    return IOPIPE_ARN_PREFIX_TEMPLATE % (get_region(), )

def get_region(region):
    boto_kwargs = {}
    if region:
        boto_kwargs['region_name'] = region
    session = boto3.session.Session(**boto_kwargs)
    return session.region_name

def get_layers(region, runtime):
    return layers.index(get_region(region), runtime)

def get_lambda_client(region):
    boto_kwargs = {}
    if region:
        boto_kwargs['region_name'] = region
    AwsLambda = boto3.client('lambda', **boto_kwargs)
    return AwsLambda

def list_functions(region, quiet, filter_choice):
    """ This lists *and* formats for display.
        it used to be in cli.py because of the display portions,
        but do-not-repeat-yourself semantics makes it easier
        to maintain it here in update.py :shrug: -Erica """
    AwsLambda = get_lambda_client(region)
    coltmpl = "{:<64}\t{:<12}\t{:>12}\n"
    conscols, consrows = shutil.get_terminal_size((80,50))
    # set all if the filter_choice is "all" or there is no filter_choice active.
    all = filter_choice == "all" or not filter_choice

    if not quiet:
        yield coltmpl.format("Function Name", "Runtime", "Installed")
        # ascii table limbo line ---
        yield ("{:-^%s}\n" % (str(conscols),)).format("")

    next_marker = None
    while True:
        list_func_args = {'MaxItems': consrows}
        if next_marker:
            list_func_args = {'Marker': next_marker, 'MaxItems': consrows}
        func_resp = AwsLambda.list_functions(**list_func_args)
        next_marker = func_resp.get("NextMarker", None)
        funcs = func_resp.get("Functions", [])

        for f in funcs:
            runtime = f.get("Runtime")
            new_handler = RUNTIME_CONFIG.get(runtime, {}).get('Handler', None)
            if f.get("Handler") == new_handler:
                f["-x-iopipe-enabled"] = True
                if not all and filter_choice != "installed":
                    continue
            elif not all and filter_choice == "installed":
                continue
            yield coltmpl.format(f.get("FunctionName"), f.get("Runtime"), f.get("-x-iopipe-enabled", False))

        if not next_marker:
            break

class MultipleLayersException(Exception):
    pass

class UpdateLambdaException(Exception):
    pass

def apply_function_api(region, function_arn, layer_arn, token, java_type):
    AwsLambda = get_lambda_client(region)
    info = AwsLambda.get_function(FunctionName=function_arn)
    runtime = info.get('Configuration', {}).get('Runtime', '')
    orig_handler = info.get('Configuration', {}).get('Handler')
    new_handler = RUNTIME_CONFIG.get(runtime, {}).get('Handler')

    if runtime.startswith('java'):
        if not java_type:
            raise UpdateLambdaException("Must specify a handler type for java functions.")
        new_handler = RUNTIME_CONFIG.get(runtime, {}).get('Handler', {}).get(java_type)

    if runtime == 'provider' or runtime not in RUNTIME_CONFIG.keys():
        raise UpdateLambdaException("Unsupported Lambda runtime: %s" % (runtime,))
    if orig_handler == new_handler:
        raise UpdateLambdaException("Already configured.")

    iopipe_layers = []
    existing_layers = []
    if layer_arn:
        iopipe_layers = [layer_arn]
    else:
        # compatible layers:
        disco_layers = get_layers(region, runtime)
        if len(iopipe_layers) > 1:
            print("Discovered layers for runtime (%s)" % (runtime,))
            for layer in disco_layers:
                print("%s\t%s" % (layer.get("LatestMatchingVersion", {}).get("LayerVersionArn", ""), layer.get("Description", "")))
            raise MultipleLayersException()
        iopipe_layers = [ disco_layers[0].get("LatestMatchingVersion", {}).get("LayerVersionArn", "") ]
        existing_layers = info.get('Configuration', {}).get('Layers', [])

    update_kwargs = {
        'FunctionName': function_arn,
        'Handler': new_handler,
        'Environment': {
            'Variables': {
                'IOPIPE_TOKEN': token
            }
        },
        'Layers': iopipe_layers + existing_layers
    }
    if runtime.startswith('java'):
        update_kwargs['Environment']['Variables']['IOPIPE_GENERIC_HANDLER'] = orig_handler
    else:
        update_kwargs['Environment']['Variables']['IOPIPE_HANDLER'] = orig_handler
    return AwsLambda.update_function_configuration(**update_kwargs)

def remove_function_api(region, function_arn, layer_arn):
    AwsLambda = get_lambda_client(region)
    info = AwsLambda.get_function(FunctionName=function_arn)
    runtime = info.get('Configuration', {}).get('Runtime', '')
    orig_handler = info.get('Configuration', {}).get('Handler', '')
    new_handler = RUNTIME_CONFIG.get(runtime, {}).get('Handler', None)

    if runtime == 'provider' or runtime not in RUNTIME_CONFIG.keys():
        print("Unsupported Lambda runtime: %s" % (runtime,))
        return
    if orig_handler != new_handler:
        print("IOpipe installation (via layers) not auto-detected for the specified function.")
        print("Unrecognized handler in deployed function.")
        return

    env_handler = info.get('Configuration', {}).get('Environment', {}).get('Variables', {}).get('IOPIPE_HANDLER')
    env_alt_handler = info.get('Configuration', {}).get('Environment', {}).get('Variables', {}).get('IOPIPE_GENERIC_HANDLER')
    if not env_handler or env_alt_handler:
        print("IOpipe installation (via layers) not auto-detected for the specified function.")
        print("No IOPIPE_HANDLER environment variable found.")
        return
    token = info.get('Configuration', {}).get('Environment', {}).get('Variables', {}).get('IOPIPE_TOKEN')
    try:
        del info['Configuration']['Environment']['Variables']['IOPIPE_HANDLER']
    except KeyError:
        pass
    try:
        del info['Configuration']['Environment']['Variables']['IOPIPE_GENERIC_HANDLER']
    except KeyError:
        pass
    try:
        del info['Configuration']['Environment']['Variables']['IOPIPE_TOKEN']
    except KeyError:
        pass

    layers = info.get('Configuration', {}).get('Layers', [])
    for layer_idx, layer_arn in enumerate(layers):
        if layer_arn['Arn'].startswith(get_arn_prefix()):
            del layers[layer_idx]

    return AwsLambda.update_function_configuration(
        FunctionName=function_arn,
        Handler=env_handler,
        Environment=info['Configuration']['Environment'],
        Layers=layers
    )

def get_stack_ids():
    CloudFormation = boto3.client('cloudformation')
    def stack_filter(stack_id):
        resources = CloudFormation.list_stack_resources(
            StackName=stack_id
        )
        for resource in resources['StackResourceSummaries']:
            if resource['ResourceType'] == 'LambdaResourceType-PLACEHOLDER':
                return True
    def map_stack_ids():
        for stack in stacks['StackSummaries']:
            return stack['StackId']

    token = None
    stack_id_pages = []
    while True:
        stacks = CloudFormation.list_stacks(NextToken=token)
        stack_id_pages += map(map_stack_ids, stacks)
        token = stacks['NextToken']
        if not token:
            break
    return filter(stack_filter, itertools.chain(*stack_id_pages))

def get_template(stackid):
    CloudFormation = boto3.client('cloudformation')
    # DOC get_template: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/cloudformation.html#CloudFormation.Client.get_template
    template_body = CloudFormation.get_template(StackName=stackid)
    #    example_get_template_body = '''
    #    {
    #    'TemplateBody': {},
    #    'StagesAvailable': [
    #        'Original'|'Processed',
    #    ]
    #    }
    #    '''
    return template_body #apply_function_cloudformation(template_body)

def modify_cloudformation(template_body, function_arn, token):
    ##runtime = info.get('Configuration', {}).get('Runtime', '')
    ##orig_handler = info.get('Configuration', {}).get('Handler', '')
    func_template = template_body.get('Resources', {}).get(function_arn, {})
    orig_handler = func_template.get('Properties', {}).get('Handler', None)
    runtime = func_template.get('Properties', {}).get('Runtime', None)
    new_handler = RUNTIME_CONFIG.get(runtime, {}).get('Handler', None)

    if runtime == 'provider' or runtime not in RUNTIME_CONFIG.keys():
        raise UpdateLambdaException("Unsupported Lambda runtime: %s" % (runtime,))
    if orig_handler == new_handler:
        raise UpdateLambdaException("Already configured.")

    updates = {
        'Resources': {
            function_arn: {
                'Properties': {
                    'Handler': new_handler
                },
                'Environment': {
                    'Variables': {
                        'IOPIPE_HANDLER': orig_handler,
                        'IOPIPE_TOKEN': token 
                    }
                }
            }
        }
    }
    #context = DeepChainMap({}, updates, template_body)
    context = combine_dict(template_body, updates)
    return context

def update_cloudformation_file(filename, function_arn, output, token):
    # input options to support:
    # - cloudformation template file (json and yaml)
    # - cloudformation stack (deployed on AWS)
    # - SAM file
    # - Serverless.yml
    orig_template_body=""
    with open(filename) as yml:
        orig_template_body=json.loads(yml.read())
    cf_template = modify_cloudformation(orig_template_body, function_arn, token)
    if output == "-":
        print(json.dumps(cf_template, indent=2))
    else:
        with open(output, 'w') as yml:
            yml.write(json.dumps(cf_template, indent=2))

def update_cloudformation_stack(stack_id, function_arn):
    CloudFormation = boto3.client('cloudformation')

    #stackid = get_stack_ids(function_arn)
    orig_template=get_template(stack_id)
    template_body=modify_cloudformation(orig_template, function_arn)
    # DOC update_stack: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/cloudformation.html#CloudFormation.Client.update_stack
    CloudFormation.update_stack(
        StackName=stack_id,
        TemplateBody=template_body
    )
