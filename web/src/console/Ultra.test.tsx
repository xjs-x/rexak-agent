// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { ExecutionModeSelect } from './Ultra';

afterEach(cleanup);
test('mode selector defaults to persisted Standard and selects Ultra', () => {
  const changed = vi.fn();
  render(<ExecutionModeSelect value="standard" onChange={changed} />);
  expect((screen.getByLabelText('运行模式') as HTMLSelectElement).value).toBe('standard');
  fireEvent.change(screen.getByLabelText('运行模式'), { target: { value: 'ultra' } });
  expect(changed).toHaveBeenCalledWith('ultra');
});
test('created session mode is fixed', () => {
  render(<ExecutionModeSelect value="ultra" onChange={vi.fn()} disabled />);
  expect((screen.getByLabelText('运行模式') as HTMLSelectElement).disabled).toBe(true);
});
