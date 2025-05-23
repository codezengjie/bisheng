import inspect
import json
from typing import TYPE_CHECKING, Any, Callable, Dict, Sequence, Type

import httpx
import openai
from bisheng.cache.utils import file_download
from bisheng.database.models.knowledge import KnowledgeDao
from bisheng.interface.agents.base import agent_creator
from bisheng.interface.chains.base import chain_creator
from bisheng.interface.custom_lists import CUSTOM_NODES
from bisheng.interface.embeddings.custom import FakeEmbedding
from bisheng.interface.importing.utils import (eval_custom_component_code, get_function,
                                               import_by_type)
from bisheng.interface.initialize.llm import initialize_vertexai
from bisheng.interface.initialize.utils import (handle_format_kwargs, handle_node_type,
                                                handle_partial_variables, langchain_bug_openv1)
from bisheng.interface.initialize.vector_store import vecstore_initializer
from bisheng.interface.output_parsers.base import output_parser_creator
from bisheng.interface.retrievers.base import retriever_creator
from bisheng.interface.toolkits.base import toolkits_creator
from bisheng.interface.utils import load_file_into_dict
from bisheng.interface.wrappers.base import wrapper_creator
from bisheng.settings import settings
from bisheng.utils import validate
from bisheng.utils.constants import NODE_ID_DICT, PRESET_QUESTION
from bisheng.utils.embedding import decide_embeddings
from bisheng_langchain.vectorstores import VectorStoreFilterRetriever
from langchain.agents import agent as agent_module
from langchain.agents.agent import AgentExecutor
from langchain.agents.agent_toolkits.base import BaseToolkit
from langchain.agents.tools import BaseTool
from langchain.chains.base import Chain
from langchain.document_loaders.base import BaseLoader
from langchain.vectorstores.base import VectorStore
from langchain_community.utils.openai import is_openai_v1
from loguru import logger
from pydantic import SecretStr, ValidationError, create_model
from pydantic.fields import FieldInfo

if TYPE_CHECKING:
    from bisheng import CustomComponent


def build_vertex_in_params(params: Dict) -> Dict:
    from bisheng.graph.vertex.base import Vertex

    # If any of the values in params is a Vertex, we will build it
    return {
        key: value.build() if isinstance(value, Vertex) else value
        for key, value in params.items()
    }


# from bisheng_langchain.document_loaders.elem_unstrcutured_loader import ElemUnstructuredLoaderV0
async def instantiate_class(node_type: str, base_type: str, params: Dict, user_id=None) -> Any:
    """Instantiate class from module type and key, and params"""
    params = convert_params_to_sets(params)
    params = convert_kwargs(params)
    params_node_id_dict = params.pop(NODE_ID_DICT)
    if node_type in CUSTOM_NODES:
        if custom_node := CUSTOM_NODES.get(node_type):
            if hasattr(custom_node, 'initialize'):
                return custom_node.initialize(**params)
            return custom_node(**params)

    class_object = import_by_type(_type=base_type, name=node_type)
    return await instantiate_based_on_type(class_object,
                                           base_type,
                                           node_type,
                                           params,
                                           params_node_id_dict,
                                           user_id=user_id)


def convert_params_to_sets(params):
    """Convert certain params to sets"""
    if 'allowed_special' in params:
        params['allowed_special'] = set(params['allowed_special'])
    if 'disallowed_special' in params:
        params['disallowed_special'] = set(params['disallowed_special'])
    if 'input_node' in params:
        params.pop('input_node')
    return params


def convert_kwargs(params):
    # if *kwargs are passed as a string, convert to dict
    # first find any key that has kwargs or config in it
    kwargs_keys = [key for key in params.keys() if 'kwargs' in key or 'config' in key]
    for key in kwargs_keys:
        if isinstance(params[key], str):
            params[key] = json.loads(params[key])
    return params


async def instantiate_based_on_type(class_object,
                                    base_type,
                                    node_type,
                                    params,
                                    param_id_dict,
                                    user_id=None):
    if base_type == 'agents':
        return instantiate_agent(node_type, class_object, params)
    elif base_type == 'prompts':
        return instantiate_prompt(node_type, class_object, params, param_id_dict)
    elif base_type == 'tools':
        tool = instantiate_tool(node_type, class_object, params)
        if hasattr(tool, 'name') and isinstance(tool, BaseTool):
            # tool name shouldn't contain spaces
            tool.name = tool.name.replace(' ', '_')
        return tool
    elif base_type == 'toolkits':
        return instantiate_toolkit(node_type, class_object, params)
    elif base_type == 'embeddings':
        return instantiate_embedding(class_object, params)
    elif base_type == 'vectorstores':
        return instantiate_vectorstore(node_type, class_object, params)
    elif base_type == 'documentloaders':
        return instantiate_documentloader(class_object, params)
    elif base_type == 'textsplitters':
        return instantiate_textsplitter(class_object, params)
    elif base_type == 'utilities':
        return instantiate_utility(node_type, class_object, params)
    elif base_type == 'chains':
        return instantiate_chains(node_type, class_object, params, param_id_dict)
    elif base_type == 'output_parsers':
        return instantiate_output_parser(node_type, class_object, params)
    elif base_type == 'llms':
        return instantiate_llm(node_type, class_object, params)
    elif base_type == 'retrievers':
        return instantiate_retriever(node_type, class_object, params)
    elif base_type == 'memory':
        return instantiate_memory(node_type, class_object, params)
    elif base_type == 'custom_components':
        return await instantiate_custom_component(node_type, class_object, params, user_id)
    elif base_type == 'wrappers':
        return instantiate_wrapper(node_type, class_object, params)
    elif base_type == 'input_output':
        return instantiate_input_output(node_type, class_object, params, param_id_dict)
    elif base_type == 'autogen_roles':
        return instantiate_autogen_roles(node_type, class_object, params)
    else:
        return class_object(**params)


async def instantiate_custom_component(node_type, class_object, params, user_id):
    params_copy = params.copy()
    class_object: 'CustomComponent' = eval_custom_component_code(params_copy.pop('code'))
    custom_component = class_object(user_id=user_id)

    if 'retriever' in params_copy and hasattr(params_copy['retriever'], 'as_retriever'):
        params_copy['retriever'] = params_copy['retriever'].as_retriever()

    # Determine if the build method is asynchronous
    is_async = inspect.iscoroutinefunction(custom_component.build)

    if is_async:
        # Await the build method directly if it's async
        built_object = await custom_component.build(**params_copy)
    else:
        # Call the build method directly if it's sync
        built_object = custom_component.build(**params_copy)

    return built_object, {'repr': custom_component.custom_repr()}


def instantiate_input_output(node_type, class_object, params, id_dict):
    if node_type == 'Report':
        preset_question = {}
        if PRESET_QUESTION in params:
            preset_question = params.pop(PRESET_QUESTION)
        chains = params.get('chains', [])
        chains_idlist = id_dict.get('chains', [])
        # 需要对chains对象进行丰富处理
        chain_list = []
        for index, id in enumerate(chains_idlist):
            chain_obj = {}
            chain_obj['object'] = chains[index]
            if id in preset_question:
                if isinstance(preset_question[id], list):
                    for node_id in preset_question[id]:
                        chain_ = chain_obj.copy()
                        chain_['node_id'] = node_id[0]
                        chain_['input'] = {chains[index].input_keys[0]: node_id[1]}
                        chain_list.append(chain_)
                    continue
                else:
                    chain_obj['node_id'] = preset_question[id][0]
                    chain_obj['input'] = {chains[index].input_keys[0]: preset_question[id][1]}
            else:
                # give a default input
                logger.error(f'Report has no question id={id}')
                chain_obj['input'] = {chains[index].input_keys[0]: 'start'}
            chain_list.append(chain_obj)
        params['chains'] = chain_list
        # variable
        variable = params.get('variables')
        variable_node_id = id_dict.get('variables') or []
        params['variables'] = []
        for index, id in enumerate(variable_node_id):
            params['variables'].append({'node_id': id, 'input': variable[index]})
        return class_object(**params)
    if node_type == 'InputFileNode':
        file_path = class_object(**params).text()
        if file_path:
            file_path, file_name2 = file_download(file_path[0])
            return [file_path, file_name2 if file_name2 else file_path[1]]
        else:
            return ''
    if 'file_path' in params:
        file_path = params['file_path']
        if not file_path:
            return ''
        if isinstance(file_path, list):
            params['file_path'] = file_path[0]

    return class_object(**params).text()


def instantiate_autogen_roles(node_type, class_object, params):
    return class_object(**params)


def instantiate_wrapper(node_type, class_object, params):
    if node_type in wrapper_creator.from_method_nodes:
        method = wrapper_creator.from_method_nodes[node_type]
        if class_method := getattr(class_object, method, None):
            return class_method(**params)
        raise ValueError(f'Method {method} not found in {class_object}')
    if node_type == 'DallEAPIWrapper' and is_openai_v1():
        if 'openai_proxy' in params and params['openai_proxy']:
            client_params = langchain_bug_openv1(params)
            client_params['http_client'] = httpx.Client(proxies=params.get('openai_proxy'))
            params['client'] = openai.OpenAI(**client_params).images
            client_params['http_client'] = httpx.AsyncClient(proxies=params.get('openai_proxy'))
            params['async_client'] = openai.AsyncOpenAI(**client_params).images

    return class_object(**params)


def instantiate_output_parser(node_type, class_object, params):
    if node_type in output_parser_creator.from_method_nodes:
        method = output_parser_creator.from_method_nodes[node_type]
        if class_method := getattr(class_object, method, None):
            return class_method(**params)
        raise ValueError(f'Method {method} not found in {class_object}')
    return class_object(**params)


def instantiate_llm(node_type, class_object, params: Dict, user_llm_request: bool = True):
    # This is a workaround so JinaChat works until streaming is implemented
    # if "openai_api_base" in params and "jina" in params["openai_api_base"]:
    # False if condition is True
    if is_openai_v1() and params.get('openai_proxy'):
        params['http_client'] = httpx.Client(proxies=params.get('openai_proxy'))
        params['http_async_client'] = httpx.AsyncClient(proxies=params.get('openai_proxy'))
        del params['openai_proxy']

    if node_type == '':
        anthropic_api_key = params.pop('anthropic_api_key', None)
        params['anthropic_api_key'] = SecretStr(anthropic_api_key) if anthropic_api_key else None

    if node_type == 'VertexAI':
        return initialize_vertexai(class_object=class_object, params=params)
    # max_tokens sometimes is a string and should be an int
    if 'max_tokens' in params:
        if isinstance(params['max_tokens'], str) and params['max_tokens'].isdigit():
            params['max_tokens'] = int(params['max_tokens'])
        elif not isinstance(params.get('max_tokens'), int):
            params.pop('max_tokens', None)

    llm = class_object(**params)
    llm_config = settings.get_from_db('llm_request')
    # 支持request_timeout & max_retries
    if hasattr(llm, 'request_timeout') and 'request_timeout' in llm_config:
        if isinstance(llm_config.get('request_timeout'), str):
            llm.request_timeout = int(llm_config.get('request_timeout'))
        else:
            llm.request_timeout = llm_config.get('request_timeout')
    if hasattr(llm, 'max_retries') and 'max_retries' in llm_config:
        llm.max_retries = llm_config.get('max_retries')

    return llm


def instantiate_memory(node_type, class_object, params):
    # process input_key and output_key to remove them if
    # they are empty strings
    if node_type == 'ConversationEntityMemory':
        params.pop('memory_key', None)

    for key in ['input_key', 'output_key']:
        if key in params and (params[key] == '' or not params[key]):
            params.pop(key)

    try:
        if 'retriever' in params and hasattr(params['retriever'], 'as_retriever'):
            params['retriever'] = params['retriever'].as_retriever()
        return class_object(**params)
    # I want to catch a specific attribute error that happens
    # when the object does not have a cursor attribute
    except Exception as exc:
        if "object has no attribute 'cursor'" in str(exc) or 'object has no field "conn"' in str(
                exc):
            raise AttributeError(
                ('Failed to build connection to database.'
                 f' Please check your connection string and try again. Error: {exc}')) from exc
        raise exc


def instantiate_retriever(node_type, class_object, params):
    for key, value in params.items():
        if 'retriever' in key and hasattr(value, 'as_retriever'):
            params[key] = value.as_retriever()
    if node_type in retriever_creator.from_method_nodes:
        method = retriever_creator.from_method_nodes[node_type]
        if class_method := getattr(class_object, method, None):
            return class_method(**params)
        raise ValueError(f'Method {method} not found in {class_object}')
    return class_object(**params)


def instantiate_chains(node_type, class_object: Type[Chain], params: Dict, id_dict: Dict):
    if 'retriever' in params:
        user_name = params.pop('user_name', '')
        if hasattr(params['retriever'], 'as_retriever'):
            if settings.get_from_db('file_access'):
                # need to verify file access
                access_url = settings.get_from_db('file_access') + f'?username={user_name}'
                logger.info('file_access_filter url={}', access_url)
                vectorstore = VectorStoreFilterRetriever(vectorstore=params['retriever'],
                                                         access_url=access_url)
            else:
                vectorstore = params['retriever'].as_retriever()
            params['retriever'] = vectorstore
    # sequence chain
    if node_type == 'SequentialChain':
        # 改造sequence 支持自定义chain顺序
        params.pop('input_node', '')  # sequential 不支持增加入参
        try:
            chain_order = json.loads(params.pop('chain_order'))
        except Exception:
            raise Exception('chain_order 不是标准数组')
        chains_origin = params.get('chains')
        chains_dict = {id: index for index, id in enumerate(id_dict.get('chains'))}
        params['chains'] = [chains_origin[chains_dict.get(id)] for id in chain_order]
    # dict 转换
    if 'headers' in params and isinstance(params['headers'], str):
        params['headers'] = json.loads(params['headers'])
    if node_type == 'ConversationalRetrievalChain':
        params['get_chat_history'] = str
        params['combine_docs_chain_kwargs'] = {
            'prompt': params.pop('combine_docs_chain_kwargs', None),
            'document_prompt': params.pop('document_prompt', None)
        }
        params['combine_docs_chain_kwargs'] = {
            k: v
            for k, v in params['combine_docs_chain_kwargs'].items() if v is not None
        }
    # 人工组装MultiPromptChain
    if node_type in {'MultiPromptChain', 'MultiRuleChain'}:
        destination_chain_name = params['destination_chain_name']
        llm_chains = params['LLMChains']
        destination_chain = {}
        i = 0
        for k, name in destination_chain_name.items():
            destination_chain[name] = llm_chains[i]
            i = i + 1
        params.pop('LLMChains')
        params.pop('destination_chain_name')
        params['destination_chains'] = destination_chain
    if node_type in chain_creator.from_method_nodes:
        method = chain_creator.from_method_nodes[node_type]
        if class_method := getattr(class_object, method, None):
            return class_method(**params)
        raise ValueError(f'Method {method} not found in {class_object}')
    return class_object(**params)


def instantiate_agent(node_type, class_object: Type[agent_module.Agent], params: Dict):
    if node_type in agent_creator.from_method_nodes:
        method = agent_creator.from_method_nodes[node_type]
        if class_method := getattr(class_object, method, None):
            agent = class_method(**params)
            tools = params.get('tools', [])
            return AgentExecutor.from_agent_and_tools(agent=agent,
                                                      tools=tools,
                                                      handle_parsing_errors=True)
    return load_agent_executor(class_object, params)


def instantiate_prompt(node_type, class_object, params: Dict, param_id_dict: Dict):
    params, prompt = handle_node_type(node_type, class_object, params)
    format_kwargs = handle_format_kwargs(prompt, params)
    # Now we'll use partial_format to format the prompt
    if format_kwargs:
        prompt = handle_partial_variables(prompt, format_kwargs)

    no_human_input = set(param_id_dict.keys())
    human_input = set(prompt.input_variables).difference(no_human_input)
    order_input = list(human_input) + list(set(prompt.input_variables) & no_human_input)
    if len(order_input) > 1:
        # if node_type == 'ChatPromptTemplate':

        if hasattr(prompt, 'prompt') and hasattr(prompt.prompt, 'input_variables'):
            prompt.prompt.input_variables = order_input
        elif hasattr(prompt, 'input_variables'):
            prompt.input_variables = order_input
    return prompt, format_kwargs


def instantiate_tool(node_type, class_object: Type[BaseTool], params: Dict):
    # build args_schema
    args_schema = params.pop('args_schema', '')
    if node_type == 'JsonSpec':
        if file_dict := load_file_into_dict(params.pop('path')):
            params['dict_'] = file_dict
        else:
            raise ValueError('Invalid file')
        return class_object(**params)
    elif node_type == 'PythonFunctionTool':
        params['func'] = get_function(params.get('code'))
        return class_object(**params)
    elif node_type == 'PythonFunction':
        function_string = params['code']
        if isinstance(function_string, str):
            return validate.eval_function(function_string)
        raise ValueError('Function should be a string')
    elif node_type.lower() == 'tool':
        tool = class_object(**params)
    tool = class_object(**params)
    if args_schema and hasattr(tool, 'args_schema'):
        fields = {}
        for name, prop in args_schema.items():
            # eval函数用于执行一个字符串表达式并返回结果
            import typing  # noqa
            if prop.get('type') == 'string':
                field_type = str
            else:
                field_type = typing.Any
            fields[name] = (field_type, FieldInfo(**prop))

        tool.args_schema = create_model(name, **fields)
    return tool


def instantiate_toolkit(node_type, class_object: Type[BaseToolkit], params: Dict):
    loaded_toolkit = class_object(**params)
    # Commenting this out for now to use toolkits as normal tools
    # if toolkits_creator.has_create_function(node_type):
    #     return load_toolkits_executor(node_type, loaded_toolkit, params)
    if isinstance(loaded_toolkit, BaseToolkit):
        return loaded_toolkit.get_tools()
    return loaded_toolkit


def instantiate_embedding(class_object, params: Dict):
    # params.pop('model', None)
    try:
        if params.get('openai_proxy'):
            params['http_client'] = httpx.Client(proxies=params.get('openai_proxy'))
            params['http_async_client'] = httpx.AsyncClient(proxies=params.get('openai_proxy'))
            del params['openai_proxy']
        if class_object.__name__ == 'OpenAIEmbeddings':
            params['check_embedding_ctx_length'] = False

        return class_object(**params)
    except ValidationError:
        params = {key: value for key, value in params.items() if key in class_object.__fields__}
        return class_object(**params)


def instantiate_vectorstore(node_type: str, class_object: Type[VectorStore], params: Dict):
    user_name = params.pop('user_name', '')
    search_kwargs = params.pop('search_kwargs', {})
    search_type = params.pop('search_type', 'similarity')
    if 'documents' not in params:
        params['documents'] = []

    # 过滤掉用户没有权限的知识库
    # TODO zgq 后续统一技能执行流程后将和业务有关的逻辑都迁移到初始化技能对象之前
    if node_type == 'MilvusWithPermissionCheck' or node_type == 'ElasticsearchWithPermissionCheck':
        col_name = 'collection_name'
        if node_type == 'ElasticsearchWithPermissionCheck':
            col_name = 'index_name'

        # 获取执行用户 有权限查看的知识库列表
        knowledge_ids = [one['key'] for one in params[col_name]]
        if params.pop('_is_check_auth', True):
            knowledge_list = KnowledgeDao.judge_knowledge_permission(user_name, knowledge_ids)
        else:
            knowledge_list = KnowledgeDao.get_list_by_ids(knowledge_ids)
        logger.debug(f'{node_type} after filter, get knowledge_list: {knowledge_list}')

        if not knowledge_list:
            logger.warning(f'{node_type}: after filter, get zero knowledge')

        # 没有任何知识库的话，提供假的embedding和空的collection_name
        if node_type == 'MilvusWithPermissionCheck':
            params[col_name] = []
            params['collection_embeddings'] = []
            params['partition_keys'] = []
            for knowledge in knowledge_list:
                params[col_name].append(knowledge.collection_name)
                params['collection_embeddings'].append(decide_embeddings(knowledge.model))
                if knowledge.collection_name.startswith('partition'):
                    params['partition_keys'].append(knowledge.id)
                else:
                    params['partition_keys'].append(None)
            params['embedding'] = params['collection_embeddings'][0] if params['collection_embeddings'] else FakeEmbedding()
        else:
            params[col_name] = [
                knowledge.index_name or knowledge.collection_name for knowledge in knowledge_list
            ]

    if initializer := vecstore_initializer.get(class_object.__name__):
        vecstore = initializer(class_object, params, search_kwargs)
    else:
        if 'texts' in params:
            params['documents'] = params.pop('texts')
        vecstore = class_object.from_documents(**params)

    # ! This might not work. Need to test
    if search_kwargs and hasattr(vecstore, 'as_retriever'):
        if settings.get_from_db('file_access'):
            # need to verify file access / 只针对知识库
            access_url = settings.get_from_db('file_access') + f'?username={user_name}'
            vecstore = VectorStoreFilterRetriever(vectorstore=vecstore,
                                                  search_type=search_type,
                                                  search_kwargs=search_kwargs,
                                                  access_url=access_url)
        else:
            vecstore = vecstore.as_retriever(search_type=search_type, search_kwargs=search_kwargs)

    return vecstore


def instantiate_documentloader(class_object: Type[BaseLoader], params: Dict):
    if 'file_filter' in params:
        # file_filter will be a string but we need a function
        # that will be used to filter the files using file_filter
        # like lambda x: x.endswith(".txt") but as we don't know
        # anything besides the string, we will simply check if the string is
        # in x and if it is, we will return True
        file_filter = params.pop('file_filter')
        extensions = file_filter.split(',')
        params['file_filter'] = lambda x: any(extension.strip() in x for extension in extensions)
    if 'file_path' in params:
        file_path = params['file_path']
        if isinstance(file_path, list):
            file_name = file_path[1]
            params['file_path'] = file_path[0]
            if class_object.__name__ == 'ElemUnstructuredLoaderV0':
                params['file_name'] = file_name
    metadata = params.pop('metadata', None)
    if metadata and isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError as exc:
            raise ValueError('The metadata you provided is not a valid JSON string.') from exc
    # make it success when file not present
    if 'file_path' in params and not params['file_path']:
        return []

    docs = class_object(**params).load()
    # Now if metadata is an empty dict, we will not add it to the documents
    if metadata:
        for doc in docs:
            # If the document already has metadata, we will not overwrite it
            if not doc.metadata:
                doc.metadata = metadata
            else:
                doc.metadata.update(metadata)

    return docs


def instantiate_textsplitter(
    class_object,
    params: Dict,
):
    try:
        documents = params.pop('documents')
        if not documents:
            return []
    except KeyError as exc:
        raise ValueError('The source you provided did not load correctly or was empty.'
                         'Try changing the chunk_size of the Text Splitter.') from exc

    if ('separator_type' in params
            and params['separator_type'] == 'Text') or 'separator_type' not in params:
        params.pop('separator_type', None)
        # separators might come in as an escaped string like \\n
        # so we need to convert it to a string
        if 'separators' in params:
            params['separators'] = (params['separators'].encode().decode('unicode-escape'))
        text_splitter = class_object(**params)
    else:
        from langchain.text_splitter import Language

        language = params.pop('separator_type', None)
        params['language'] = Language(language)
        params.pop('separators', None)

        text_splitter = class_object.from_language(**params)
    return text_splitter.split_documents(documents)


def instantiate_utility(node_type, class_object, params: Dict):
    if node_type == 'SQLDatabase':
        return class_object.from_uri(params.pop('uri'))
    return class_object(**params)


def replace_zero_shot_prompt_with_prompt_template(nodes):
    """Replace ZeroShotPrompt with PromptTemplate"""
    for node in nodes:
        if node['data']['type'] == 'ZeroShotPrompt':
            # Build Prompt Template
            tools = [
                tool for tool in nodes if tool['type'] != 'chatOutputNode'
                and 'Tool' in tool['data']['node']['base_classes']
            ]
            node['data'] = build_prompt_template(prompt=node['data'], tools=tools)
            break
    return nodes


def load_agent_executor(agent_class: type[agent_module.Agent], params, **kwargs):
    """Load agent executor from agent class, tools and chain"""
    allowed_tools: Sequence[BaseTool] = params.get('allowed_tools', [])
    llm_chain = params['llm_chain']
    # agent has hidden args for memory. might need to be support
    # memory = params["memory"]
    # if allowed_tools is not a list or set, make it a list
    if not isinstance(allowed_tools, (list, set)) and isinstance(allowed_tools, BaseTool):
        allowed_tools = [allowed_tools]
    tool_names = [tool.name for tool in allowed_tools]
    # Agent class requires an output_parser but Agent classes
    # have a default output_parser.
    agent = agent_class(allowed_tools=tool_names, llm_chain=llm_chain)  # type: ignore
    return AgentExecutor.from_agent_and_tools(
        agent=agent,
        tools=allowed_tools,
        handle_parsing_errors=True,
        # memory=memory,
        **kwargs,
    )


def load_toolkits_executor(node_type: str, toolkit: BaseToolkit, params: dict):
    create_function: Callable = toolkits_creator.get_create_function(node_type)
    if llm := params.get('llm'):
        return create_function(llm=llm, toolkit=toolkit)


def build_prompt_template(prompt, tools):
    """Build PromptTemplate from ZeroShotPrompt"""
    prefix = prompt['node']['template']['prefix']['value']
    suffix = prompt['node']['template']['suffix']['value']
    format_instructions = prompt['node']['template']['format_instructions']['value']

    tool_strings = '\n'.join([
        f"{tool['data']['node']['name']}: {tool['data']['node']['description']}" for tool in tools
    ])
    tool_names = ', '.join([tool['data']['node']['name'] for tool in tools])
    format_instructions = format_instructions.format(tool_names=tool_names)
    value = '\n\n'.join([prefix, tool_strings, format_instructions, suffix])

    prompt['type'] = 'PromptTemplate'

    prompt['node'] = {
        'template': {
            '_type': 'prompt',
            'input_variables': {
                'type': 'str',
                'required': True,
                'placeholder': '',
                'list': True,
                'show': False,
                'multiline': False,
            },
            'output_parser': {
                'type': 'BaseOutputParser',
                'required': False,
                'placeholder': '',
                'list': False,
                'show': False,
                'multline': False,
                'value': None,
            },
            'template': {
                'type': 'str',
                'required': True,
                'placeholder': '',
                'list': False,
                'show': True,
                'multiline': True,
                'value': value,
            },
            'template_format': {
                'type': 'str',
                'required': False,
                'placeholder': '',
                'list': False,
                'show': False,
                'multline': False,
                'value': 'f-string',
            },
            'validate_template': {
                'type': 'bool',
                'required': False,
                'placeholder': '',
                'list': False,
                'show': False,
                'multline': False,
                'value': True,
            },
        },
        'description': 'Schema to represent a prompt for an LLM.',
        'base_classes': ['BasePromptTemplate'],
    }

    return prompt
