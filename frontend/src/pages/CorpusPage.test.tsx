import { renderToString } from 'react-dom/server';
import { MemoryRouter } from 'react-router';
import { describe, expect, it } from 'vitest';

import CorpusPage from './CorpusPage';

describe('CorpusPage', () => {
  it('renders corpus controls, the start-a-project CTA, and privacy notice', () => {
    const html = renderToString(
      <MemoryRouter>
        <CorpusPage />
      </MemoryRouter>
    );

    // Default UI language is en in the test environment (no localStorage).
    expect(html).toContain('Prior papers');
    expect(html).toContain('Upload prior paper');
    expect(html).toContain('never sent to plagiarism');
    expect(html).toContain('Start a new project');
    expect(html).toContain('/runs/new');
  });
});
