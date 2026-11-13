// @vitest-environment jsdom
import { afterEach, expect, test } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { ExecutionModeLabel } from './Ultra';

afterEach(cleanup);
test('new session only displays Ultra without a mode selector', () => {
  render(<ExecutionModeLabel />);
  expect(screen.getByLabelText('运行模式').textContent).toContain('Ultra');
  expect(screen.queryByRole('combobox', { name: '运行模式' })).toBeNull();
});
