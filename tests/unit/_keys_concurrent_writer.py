import json, os, sys
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.mcp_tools import AGENT_API_KEY_ENV
from alicebot_api.onramp import sqlite_url_for_path
database, key, count, tag = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
os.environ[AGENT_API_KEY_ENV] = key
from pathlib import Path
context = MCPRuntimeContext(database_url=sqlite_url_for_path(Path(database)), user_id=sys.argv[5])
for index in range(count):
    answer = call_mcp_tool(context, name='alice_capture', arguments={
        'raw_text': f'# {tag}-{index}\n\nThe {tag} note {index} records the cadence for the {tag} archive.',
        'title': f'{tag}-{index}', 'domain': 'project', 'sensitivity': 'public',
    })
    print(json.dumps({'source_id': str(answer.get('source_id'))}))
