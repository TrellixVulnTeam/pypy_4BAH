
class AppTestCoroutine:

    def test_cannot_iterate(self): """
        async def f(x):
            pass
        raises(TypeError, "for i in f(5): pass")
        raises(TypeError, iter, f(5))
        raises(TypeError, next, f(5))
        """

    def test_async_for(self): """
        class X:
            def __aiter__(self):
                return MyAIter()
        class MyAIter:
            async def __anext__(self):
                return 42
        async def f(x):
            sum = 0
            async for a in x:
                sum += a
                if sum > 100:
                    break
            return sum
        cr = f(X())
        try:
            cr.send(None)
        except StopIteration as e:
            assert e.value == 42 * 3
        else:
            assert False, "should have raised"
        """

    def test_StopAsyncIteration(self): """
        class X:
            def __aiter__(self):
                return MyAIter()
        class MyAIter:
            count = 0
            async def __anext__(self):
                if self.count == 3:
                    raise StopAsyncIteration
                self.count += 1
                return 42
        async def f(x):
            sum = 0
            async for a in x:
                sum += a
            return sum
        cr = f(X())
        try:
            cr.send(None)
        except StopIteration as e:
            assert e.value == 42 * 3
        else:
            assert False, "should have raised"
        """

    def test_async_for_old_style(self): """
        class X:
            def __aiter__(self):
                return MyAIter()
        class MyAIter:
            def __await__(self):
                return iter([20, 30])
        async def f(x):
            sum = 0
            async for a in x:
                sum += a
                if sum > 100:
                    break
            return sum
        cr = f(X())
        assert next(cr.__await__()) == 20
        """

    def test_set_coroutine_wrapper(self): """
        import sys
        async def f():
            pass
        seen = []
        def my_wrapper(cr):
            seen.append(cr)
            return 42
        assert sys.get_coroutine_wrapper() is None
        sys.set_coroutine_wrapper(my_wrapper)
        assert sys.get_coroutine_wrapper() is my_wrapper
        cr = f()
        assert cr == 42
        sys.set_coroutine_wrapper(None)
        assert sys.get_coroutine_wrapper() is None
        """

    def test_async_with(self): """
        seen = []
        class X:
            async def __aenter__(self):
                seen.append('aenter')
            async def __aexit__(self, *args):
                seen.append('aexit')
        async def f(x):
            async with x:
                return 42
        c = f(X())
        try:
            c.send(None)
        except StopIteration as e:
            assert e.value == 42
        else:
            assert False, "should have raised"
        assert seen == ['aenter', 'aexit']
        """

    def test_async_with_exit_True(self): """
        seen = []
        class X:
            async def __aenter__(self):
                seen.append('aenter')
            async def __aexit__(self, *args):
                seen.append('aexit')
                return True
        async def f(x):
            async with x:
                return 42
        c = f(X())
        try:
            c.send(None)
        except StopIteration as e:
            assert e.value == 42
        else:
            assert False, "should have raised"
        assert seen == ['aenter', 'aexit']
        """

    def test_await(self): """
        class X:
            def __await__(self):
                i1 = yield 40
                assert i1 == 82
                i2 = yield 41
                assert i2 == 93
        async def f():
            await X()
            await X()
        c = f()
        assert c.send(None) == 40
        assert c.send(82) == 41
        assert c.send(93) == 40
        assert c.send(82) == 41
        raises(StopIteration, c.send, 93)
        """

    def test_await_error(self): """
        async def f():
            await [42]
        c = f()
        try:
            c.send(None)
        except TypeError as e:
            assert str(e) == "object list can't be used in 'await' expression"
        else:
            assert False, "should have raised"
        """

    def test_async_with_exception_context(self): """
        class CM:
            async def __aenter__(self):
                pass
            async def __aexit__(self, *e):
                1/0
        async def f():
            async with CM():
                raise ValueError
        c = f()
        try:
            c.send(None)
        except ZeroDivisionError as e:
            assert e.__context__ is not None
            assert isinstance(e.__context__, ValueError)
        else:
            assert False, "should have raised"
        """

    def test_runtime_warning(self): """
        import gc, warnings
        async def foobaz():
            pass
        with warnings.catch_warnings(record=True) as l:
            foobaz()
            gc.collect()
            gc.collect()
            gc.collect()

        assert len(l) == 1, repr(l)
        w = l[0].message
        assert isinstance(w, RuntimeWarning)
        assert str(w).startswith("coroutine ")
        assert str(w).endswith("foobaz' was never awaited")
        """

    def test_async_for_with_tuple_subclass(self): """
        class Done(Exception): pass

        class AIter(tuple):
            i = 0
            def __aiter__(self):
                return self
            async def __anext__(self):
                if self.i >= len(self):
                    raise StopAsyncIteration
                self.i += 1
                return self[self.i - 1]

        result = []
        async def foo():
            async for i in AIter([42]):
                result.append(i)
            raise Done

        try:
            foo().send(None)
        except Done:
            pass
        assert result == [42]
        """
