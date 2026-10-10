"""Isolated real UI/API fixture. Never reads user DB or calls paid AI."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import time
from dataclasses import replace
from unittest.mock import patch
from test_candidates import CandidateTests, explanation
import uvicorn

fixture = CandidateTests()
fixture.setUp()
try:
    with patch('app.db.Database',return_value=fixture.db),patch('app.config.ensure_directories'):
        import main
    main.database=fixture.db
    main._candidate_service=fixture.service()
    fixture.client.generate_candidate_explanation.side_effect=lambda text:(time.sleep(3),explanation())[1]
    main.candidate_llm_client=lambda model: fixture.client
    # Disable all unrelated scheduled collectors/sync/update checks in this fixture.
    main.app.router.on_startup.clear()
    main.app.router.on_shutdown.clear()
    main.settings=replace(main.settings,auto_sync_on_open=False,auto_close_sync=False,update_check_on_startup=False)
    @main.app.get('/test/candidate-metrics')
    def metrics():
        return {'model_calls':fixture.client.generate_candidate_explanation.call_count,'isolated':True}
    uvicorn.run(main.app,host='127.0.0.1',port=59517,log_level='warning')
finally:
    fixture.doCleanups()
