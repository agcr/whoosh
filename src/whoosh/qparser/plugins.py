# Copyright 2011 Matt Chaput. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#    1. Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#
#    2. Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY MATT CHAPUT ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO
# EVENT SHALL MATT CHAPUT OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
# EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are
# those of the authors and should not be interpreted as representing official
# policies, either expressed or implied, of Matt Chaput.

import copy

from whoosh import query
from whoosh.compat import iteritems, u
from whoosh.qparser import syntax
from whoosh.qparser.common import rcompile, xfer
from whoosh.qparser.taggers import RegexTagger, FnTagger


class Plugin(object):
    """Base class for parser plugins.
    """
    
    def taggers(self, parser):
        """Should return a list of ``(Tagger, priority)`` tuples to add to the
        syntax the parser understands. Lower priorities run first.
        """
        
        return ()
    
    def filters(self, parser):
        """Should return a list of ``(filter_function, priority)`` tuples to
        add to parser.
        
        Filter functions will be called with ``(parser, groupnode)`` and should
        return a group node.
        """
        
        return ()


class TaggingPlugin(RegexTagger):
    """A plugin that also acts as a Tagger, to avoid having an extra Tagger
    class for simple cases.
    
    A TaggingPlugin object should have a ``priority`` attribute and either a
    ``nodetype`` attribute or a ``create()`` method. If the subclass doesn't
    override ``create()``, the base class will call ``self.nodetype`` with the
    Match object's named groups as keyword arguments.
    """
    
    priority = 0
    
    def __init__(self, expr=None):
        self.expr = rcompile(expr or self.expr)
        
    def taggers(self, parser):
        return [(self, self.priority)]
    
    def filters(self, parser):
        return ()
    
    def create(self, parser, match):
        return self.nodetype(**match.groupdict())


class WhitespacePlugin(TaggingPlugin):
    """Tags whitespace and removes it at priority 500. Depending on whether
    your plugin's filter wants to see where whitespace was in the original
    query, it should run with priority lower than 500 (before removal of
    whitespace) or higher than 500 (after removal of whitespace).
    """
    
    nodetype = syntax.Whitespace
    priority = 100
    
    def __init__(self, expr=r"\s+"):
        TaggingPlugin.__init__(self, expr)
    
    def filters(self, parser):
        return [(self.remove_whitespace, 500)]
    
    def remove_whitespace(self, parser, group):
        newgroup = group.empty_copy()
        for node in group:
            if isinstance(node, syntax.GroupNode):
                newgroup.append(self.remove_whitespace(parser, node))
            elif not node.is_ws():
                newgroup.append(node)
        return newgroup


class SingleQuotePlugin(TaggingPlugin):
    """Adds the ability to specify single "terms" containing spaces by
    enclosing them in single quotes.
    """
    
    expr=r"(^|(?<=\W))'(?P<text>.*?)'(?=\s|\]|[)}]|$)"
    nodetype = syntax.WordNode
    

class PrefixPlugin(TaggingPlugin):
    """Adds the ability to specify prefix queries by ending a term with an
    asterisk.
    
    This plugin is useful if you want the user to be able to create prefix but
    not wildcard queries (for performance reasons). If you are including the
    wildcard plugin, you should not include this plugin as well.
    
    >>> qp = qparser.QueryParser("content", myschema)
    >>> qp.remove_plugin_class(qparser.WildcardPlugin)
    >>> qp.add_plugin(qparser.PrefixPlugin())
    >>> q = qp.parse("pre*")
    """
    
    class PrefixNode(syntax.TextNode):
        qclass = query.Prefix
        
        def r(self):
            return "%r*" % self.text
    
    expr="(?P<text>[^ \t\r\n*]+)[*](?= |$|\\))"
    nodetype = PrefixNode
    

class WildcardPlugin(TaggingPlugin):
    class WildcardNode(syntax.TextNode):
        qclass = query.Wildcard
        
        def r(self):
            return "Wild %r" % self.text
    
    # Any number of word chars, followed by at least one question mark or
    # star, followed by any number of word chars, question marks, or stars
    # \u055E = Armenian question mark
    # \u061F = Arabic question mark
    # \u1367 = Ethiopic question mark
    expr=u("(?P<text>\\w*[*?\u055E\u061F\u1367](\\w|[*?\u055E\u061F\u1367])*)")
    nodetype = WildcardNode


class BoostPlugin(TaggingPlugin):
    """Adds the ability to boost clauses of the query using the circumflex.
    
    >>> qp = qparser.QueryParser("content", myschema)
    >>> q = qp.parse("hello there^2")    
    """
    
    expr = "\\^(?P<boost>[0-9]*(\\.[0-9]+)?)($|(?=[ \t\r\n)]))"
    
    class BoostNode(syntax.SyntaxNode):
        def __init__(self, original, boost):
            self.original = original
            self.boost = boost
        
        def r(self):
            return "^ %s" % self.boost
    
    def create(self, parser, match):
        # Override create so we can grab group 0
        original = match.group(0)
        try:
            boost = float(match.group("boost"))
        except ValueError:
            # The text after the ^ wasn't a valid number, so turn it into a
            # word
            return syntax.WordNode(original)
        
        return self.BoostNode(original, boost)
    
    def filters(self, parser):
        return [(self.clean_boost, 0), (self.do_boost, 700)]
    
    def clean_boost(self, parser, group):
        """This filter finds any BoostNodes in positions where they can't boost
        the previous node (e.g. at the very beginning, after whitespace, or
        after another BoostNode) and turns them into WordNodes.
        """
        
        bnode = self.BoostNode
        for i, node in enumerate(group):
            if isinstance(node, bnode):
                if (not i or not group[i - 1].has_boost):
                    group[i] = syntax.WordNode(node.original)
        return group
    
    def do_boost(self, parser, group):
        """This filter finds BoostNodes and applies the boost to the previous
        node.
        """
        
        newgroup = group.empty_copy()
        for node in group:
            if isinstance(node, syntax.GroupNode):
                node = self.do_boost(parser, node)
            elif isinstance(node, self.BoostNode):
                if (newgroup and newgroup[-1].has_boost):
                    # Apply the BoostNode's boost to the previous node
                    newgroup[-1].set_boost(node.boost)
                    # Skip adding the BoostNode to the new group
                    continue
                else:
                    node = syntax.WordNode(node.original)
            
            newgroup.append(node)
        return newgroup


class GroupPlugin(Plugin):
    """Adds the ability to group clauses using parentheses.
    """
    
    # Marker nodes for open and close bracket
    
    class OpenBracket(syntax.SyntaxNode):
        def r(self):
            return "("
    
    class CloseBracket(syntax.SyntaxNode):
        def r(self):
            return ")"
    
    def __init__(self, openexpr="\\(", closeexpr="\\)"):
        self.openexpr = openexpr
        self.closeexpr = closeexpr
    
    def taggers(self, parser):
        return [(FnTagger(self.openexpr, self.OpenBracket), 0),
                (FnTagger(self.closeexpr, self.CloseBracket), 0)]
    
    def filters(self, parser):
        return [(self.do_groups, 0)]
    
    def do_groups(self, parser, group):
        """This filter finds open and close bracket markers in a flat group
        and uses them to organize the nodes into a hierarchy.
        """
        
        ob, cb = self.OpenBracket, self.CloseBracket
        # Group hierarchy stack
        stack = [parser.group()]
        for node in group:
            if isinstance(node, ob):
                # Open bracket: push a new level of hierarchy on the stack
                stack.append(parser.group())
            elif isinstance(node, cb):
                # Close bracket: pop the current level of hierarchy and append
                # it to the previous level
                if len(stack) > 1:
                    last = stack.pop()
                    stack[-1].append(last)
            else:
                # Anything else: add it to the current level of hierarchy
                stack[-1].append(node)
        
        top = stack[0]
        # If the parens were unbalanced (more opens than closes), just take
        # whatever levels of hierarchy were left on the stack and tack them on
        # the end of the top-level
        if len(stack) > 1:
            for ls in stack[1:]:
                top.extend(ls)
        
        if len(top) == 1 and isinstance(top[0], syntax.GroupNode):
            boost = top.boost
            top = top[0]
            top.boost = boost
            
        return top


class FieldsPlugin(TaggingPlugin):
    """Adds the ability to specify the field of a clause.
    """
    
    class FieldnameTagger(RegexTagger):
        def create(self, parser, match):
            return syntax.FieldnameNode(match.group("text"), match.group(0))
    
    def __init__(self, expr=r"(?P<text>\w+):", remove_unknown=True):
        """
        :param expr: the regular expression to use for tagging fields.
        :param remove_unknown: if True, converts field specifications for
            fields that aren't in the schema into regular text.
        """
        
        self.expr = expr
        self.removeunknown = remove_unknown
    
    def taggers(self, parser):
        return [(self.FieldnameTagger(self.expr), 0)]
    
    def filters(self, parser):
        return [(self.do_fieldnames, 100)]
    
    def do_fieldnames(self, parser, group):
        """This filter finds FieldnameNodes in the tree and applies their
        fieldname to the next node.
        """
        
        fnclass = syntax.FieldnameNode
        
        if self.removeunknown and parser.schema:
            # Look for field nodes that aren't in the schema and convert them
            # to text
            schema = parser.schema
            newgroup = group.empty_copy()
            text = None
            for node in group:
                if isinstance(node, fnclass) and node.fieldname not in schema:
                    text = node.original
                    continue
                elif text:
                    if node.has_text:
                        node.text = text + node.text
                    else:
                        newgroup.append(syntax.WordNode(text))
                    text = None
                
                newgroup.append(node)
            if text:
                newgroup.append(syntax.WordNode(text))
            group = newgroup
        
        newgroup = group.empty_copy()
        # Iterate backwards through the stream, looking for field-able objects
        # with field nodes in front of them
        i = len(group)
        while i > 0:
            i -= 1
            node = group[i]
            if isinstance(node, fnclass):
                node = syntax.WordNode(node.original)
            elif isinstance(node, syntax.GroupNode):
                node = self.do_fieldnames(parser, node)
            
            if i > 0 and not node.is_ws() and isinstance(group[i - 1], fnclass):
                node.set_fieldname(group[i - 1].fieldname, override=False)
                i -= 1
            
            newgroup.append(node)
        newgroup.reverse()
        return newgroup
    

class PhrasePlugin(Plugin):
    """Adds the ability to specify phrase queries inside double quotes.
    """
    
    # Didn't use TaggingPlugin because I need to add slop parsing at some
    # point
    
    class PhraseNode(syntax.TextNode):
        def __init__(self, text, slop=1):
            syntax.TextNode.__init__(self, text)
            self.slop = slop
        
        def r(self):
            return "%s %r~%s" % (self.__class__.__name__, self.text, self.slop)
        
        def apply(self, fn):
            return self.__class__(self.type, [fn(node) for node in self.nodes],
                                  slop=self.slop, boost=self.boost)
        
        def query(self, parser):
            fieldname = self.fieldname or parser.fieldname
            if parser.schema and fieldname in parser.schema:
                field = parser.schema[fieldname]
                words = list(field.process_text(self.text, mode="query"))
            else:
                words = self.text.split(" ")
            
            qclass = parser.phraseclass
            q = qclass(fieldname, words, slop=self.slop, boost=self.boost)
            return xfer(q, self)
    
    class PhraseTagger(RegexTagger):
        def create(self, parser, matcher):
            return PhrasePlugin.PhraseNode(matcher.group("text"))
    
    def __init__(self, expr='"(?P<text>.*?)"'):
        self.expr = expr
    
    def taggers(self, parser):
        return [(self.PhraseTagger(self.expr), 0)]


class RangePlugin(Plugin):
    """Adds the ability to specify term ranges.
    """
    
    expr = rcompile(r"""
    (?P<open>\{|\[)               # Open paren
    (?P<start>
        ('[^']*?'\s+)             # single-quoted 
        |                         # or
        (.+?(?=[Tt][Oo]))         # everything until "to"
    )?
    [Tt][Oo]                      # "to"
    (?P<end>
        (\s+'[^']*?')             # single-quoted
        |                         # or
        ((.+?)(?=]|}))            # everything until "]" or "}"
    )?
    (?P<close>}|])                # Close paren
    """, verbose=True)
    
    class RangeTagger(RegexTagger):
        def __init__(self, expr, excl_start, excl_end):
            self.expr = expr
            self.excl_start = excl_start
            self.excl_end = excl_end
        
        def create(self, parser, match):
            start = match.group("start")
            end = match.group("end")
            if start:
                # Strip the space before the "to"
                start = start.rstrip()
                # Strip single quotes
                if start.startswith("'") and start.endswith("'"):
                    start = start[1:-1]
            if end:
                # Strip the space before the "to"
                end = end.lstrip()
                # Strip single quotes
                if end.startswith("'") and end.endswith("'"):
                    end = end[1:-1]
            # What kind of open and close brackets were used?
            startexcl = match.group("open") == self.excl_start
            endexcl = match.group("close") == self.excl_end
            
            return syntax.RangeNode(start, end, startexcl, endexcl)
    
    def __init__(self, expr=None, excl_start="{", excl_end="}"):
        self.expr = expr or self.expr
        self.excl_start = excl_start
        self.excl_end = excl_end
    
    def taggers(self, parser):
        tagger = self.RangeTagger(self.expr, self.excl_start, self.excl_end)
        return [(tagger, 1)]
    
            
class OperatorsPlugin(Plugin):
    """By default, adds the AND, OR, ANDNOT, ANDMAYBE, and NOT operators to
    the parser syntax. This plugin scans the token stream for subclasses of
    :class:`Operator` and calls their :meth:`Operator.make_group` methods
    to allow them to manipulate the stream.
    
    There are two levels of configuration available.
    
    The first level is to change the regular expressions of the default
    operators, using the ``And``, ``Or``, ``AndNot``, ``AndMaybe``, and/or
    ``Not`` keyword arguments. The keyword value can be a pattern string or
    a compiled expression, or None to remove the operator::
    
        qp = qparser.QueryParser("content", schema)
        cp = qparser.OperatorsPlugin(And="&", Or="\\|", AndNot="&!", AndMaybe="&~", Not=None)
        qp.replace_plugin(cp)
    
    You can also specify a list of ``(OpTagger, priority)`` pairs as the first
    argument to the initializer to use custom operators. See :ref:`custom-op`
    for more information on this.
    """
    
    class OpTagger(RegexTagger):
        def __init__(self, expr, grouptype, optype=syntax.InfixOperator,
                     leftassoc=True):
            RegexTagger.__init__(self, expr)
            self.grouptype = grouptype
            self.optype = optype
            self.leftassoc = leftassoc
        
        def create(self, parser, match):
            return self.optype(match.group(0), self.grouptype, self.leftassoc)
    
    def __init__(self, ops=None, clean=False, And=r"\sAND\s", Or=r"\sOR\s",
                 AndNot=r"\sANDNOT\s", AndMaybe=r"\sANDMAYBE\s",
                 Not=r"(^|(?<= ))NOT\s", Require=r"(^|(?<= ))REQUIRE\s"):
        if ops:
            ops = list(ops)
        else:
            ops = []
        
        if not clean:
            ot = self.OpTagger
            if Not:
                ops.append((ot(Not, syntax.NotGroup, syntax.PrefixOperator), 0))
            if And:
                ops.append((ot(And, syntax.AndGroup), 0))
            if Or:
                ops.append((ot(Or, syntax.OrGroup), 0))
            if AndNot:
                ops.append((ot(AndNot, syntax.AndNotGroup), -5))
            if AndMaybe:
                ops.append((ot(AndMaybe, syntax.AndMaybeGroup), -5))
            if Require:
                ops.append((ot(Require, syntax.RequireGroup), 0))
        
        self.ops = ops
    
    def taggers(self, parser):
        return self.ops
    
    def filters(self, parser):
        return [(self.do_operators, 600)]
    
    def do_operators(self, parser, group):
        """This filter finds PrefixOperator, PostfixOperator, and InfixOperator
        nodes in the tree and calls their logic to rearrange the nodes.
        """
        
        for tagger, _ in self.ops:
            # Get the operators created by the configured taggers
            optype = tagger.optype
            gtype = tagger.grouptype
            
            # Left-associative infix operators are replaced left-to-right, and
            # right-associative infix operators are replaced right-to-left.
            # Most of the work is done in the different implementations of
            # Operator.replace_self().
            if tagger.leftassoc:
                i = 0
                while i < len(group):
                    t = group[i]
                    if isinstance(t, optype) and t.grouptype is gtype:
                        i = t.replace_self(parser, group, i)
                    else:
                        i += 1
            else:
                i = len(group) - 1
                while i >= 0:
                    t = group[i]
                    if isinstance(t, optype):
                        i = t.replace_self(parser, group, i)
                    i -= 1
        
        # Descend into the groups and recursively call do_operators
        for i, t in enumerate(group):
            if isinstance(t, syntax.GroupNode):
                group[i] = self.do_operators(parser, t)
        
        return group


#

class PlusMinusPlugin(Plugin):
    """Adds the ability to use + and - in a flat OR query to specify required
    and prohibited terms.
    
    This is the basis for the parser configuration returned by
    ``SimpleParser()``.
    """
    
    # Marker nodes for + and -
    
    class Plus(syntax.MarkerNode): pass
    class Minus(syntax.MarkerNode): pass
    
    def __init__(self, plusexpr="\\+", minusexpr="-"):
        self.plusexpr = plusexpr
        self.minusexpr = minusexpr
    
    def taggers(self, parser):
        return [(FnTagger(self.plusexpr, self.Plus), 0),
                (FnTagger(self.minusexpr, self.Minus), 0)]
    
    def filters(self, parser):
        return [(self.do_plusminus, 510)]
    
    def do_plusminus(self, parser, group):
        """This filter sorts nodes in a flat group into "required", "optional",
        and "banned" subgroups based on the presence of plus and minus nodes.
        """
        
        required = syntax.AndGroup()
        optional = syntax.OrGroup()
        banned = syntax.OrGroup()

        # Which group to put the next node we see into
        next = optional
        for node in group:
            if isinstance(node, self.Plus):
                # +: put the next node in the required group
                next = required
            elif isinstance(node, self.Minus):
                # -: put the next node in the banned group
                next = banned
            else:
                # Anything else: put it in the appropriate group
                next.append(node)
                # Reset to putting things in the optional group by default
                next = optional
        
        group = optional
        if required:
            group = syntax.AndMaybeGroup([required, group])
        if banned:
            group = syntax.AndNotGroup([group, banned])
        return group


class GtLtPlugin(TaggingPlugin):
    """Allows the user to use greater than/less than symbols to create range
    queries::
    
        a:>100 b:<=z c:>=-1.4 d:<mz
        
    This is the equivalent of::
    
        a:{100 to] b:[to z] c:[-1.4 to] d:[to mz}
        
    The plugin recognizes ``>``, ``<``, ``>=``, ``<=``, ``=>``, and ``=<``
    after a field specifier. The field specifier is required. You cannot do the
    following::
    
        >100
        
    This plugin requires the FieldsPlugin and RangePlugin to work.
    """
    
    class GtLtNode(syntax.SyntaxNode):
        def __init__(self, rel):
            self.rel = rel
        
        def __repr__(self):
            return "(%s)" % self.rel
    
    expr=r"(?P<rel>(<=|>=|<|>|=<|=>))"
    nodetype = GtLtNode
    
    def filters(self, parser):
        # Run before the fields filter removes FilenameNodes at priority 100.
        return [(self.do_gtlt, 99)]
    
    def do_gtlt(self, parser, group):
        """This filter translate FieldnameNode/GtLtNode pairs into RangeNodes.
        """
        
        fname = syntax.FieldnameNode
        newgroup = group.empty_copy()
        i = 0
        lasti = len(group) - 1
        while i < len(group):
            node = group[i]
            # If this is a GtLtNode...
            if isinstance(node, self.GtLtNode):
                # If it's not the last node in the group...
                if i < lasti:
                    prevnode = newgroup[-1]
                    nextnode = group[i + 1]
                    # If previous was a fieldname and next node has text
                    if isinstance(prevnode, fname) and nextnode.has_text:
                        # Make the next node into a range based on the symbol
                        newgroup.append(self.make_range(nextnode, node.rel))
                        # Skip the next node
                        i += 1
            else:
                # If it's not a GtLtNode, add it to the filtered group
                newgroup.append(node)
            i += 1
        
        return newgroup
            
    def make_range(self, node, rel):
        text = node.text
        if rel == "<":
            n = syntax.RangeNode(None, text, False, True)
        elif rel == ">":
            n = syntax.RangeNode(text, None, True, False)
        elif rel == "<=" or rel == "=<":
            n = syntax.RangeNode(None, text, False, False)
        elif rel == ">=" or rel == "=>":
            n = syntax.RangeNode(text, None, False, False)
        n.startchar = node.startchar
        n.endchar = node.endchar
        return n


class MultifieldPlugin(Plugin):
    """Converts any unfielded terms into OR clauses that search for the
    term in a specified list of fields.
    
    >>> qp = qparser.QueryParser(None, myschema)
    >>> qp.add_plugin(qparser.MultifieldPlugin(["a", "b"])
    >>> qp.parse("alfa c:bravo")
    And([Or([Term("a", "alfa"), Term("b", "alfa")]), Term("c", "bravo")])
    
    This plugin is the basis for the ``MultifieldParser``.
    """
    
    def __init__(self, fieldnames, fieldboosts=None, group=syntax.OrGroup):
        """
        :param fieldnames: a list of fields to search.
        :param fieldboosts: an optional dictionary mapping field names to
            a boost to use for that field.
        :param group: the group to use to relate the fielded terms to each
            other.
        """
        
        self.fieldnames = fieldnames
        self.boosts = fieldboosts or {}
        self.group = group
    
    def filters(self, parser):
        # Run after the fields filter applies explicit fieldnames (at priority
        # 100)
        return [(self.do_multifield, 110)]
    
    def do_multifield(self, parser, group):
        for i, node in enumerate(group):
            if isinstance(node, syntax.GroupNode):
                # Recurse inside groups
                group[i] = self.do_multifield(parser, node)
            elif node.has_fieldname and node.fieldname is None:
                # For an unfielded node, create a new group containing fielded
                # versions of the node for each configured "multi" field.
                newnodes = []
                for fname in self.fieldnames:
                    newnode = copy.copy(node)
                    newnode.set_fieldname(fname)
                    newnode.set_boost(self.boosts.get(fname, 1.0))
                    newnodes.append(newnode)
                group[i] = self.group(newnodes)
        return group


class FieldAliasPlugin(Plugin):
    """Adds the ability to use "aliases" of fields in the query string.
    
    This plugin is useful for allowing users of languages that can't be
    represented in ASCII to use field names in their own language, and
    translate them into the "real" field names, which must be valid Python
    identifiers.
    
    >>> # Allow users to use 'body' or 'text' to refer to the 'content' field
    >>> parser.add_plugin(FieldAliasPlugin({"content": ["body", "text"]}))
    >>> parser.parse("text:hello")
    Term("content", "hello")
    """
    
    def __init__(self, fieldmap):
        self.fieldmap = fieldmap
        self.reverse = {}
        for key, values in iteritems(fieldmap):
            for value in values:
                self.reverse[value] = key
    
    def filters(self, parser):
        return [(self.do_aliases, 90)]
    
    def do_aliases(self, parser, group):
        for i, node in enumerate(group):
            if isinstance(node, syntax.GroupNode):
                group[i] = self.do_aliases(parser, node)
            elif node.has_fieldname and node.fieldname is not None:
                fname = node.fieldname
                if fname in self.reverse:
                    node.set_fieldname(self.reverse[fname], override=True)
        return group


class CopyFieldPlugin(Plugin):
    """Looks for basic syntax nodes (terms, prefixes, wildcards, phrases, etc.)
    occurring in a certain field and replaces it with a group (by default OR)
    containing the original token and the token copied to a new field.
    
    For example, the query::
    
        hello name:matt
        
    could be automatically converted by ``CopyFieldPlugin({"name", "author"})``
    to::
    
        hello (name:matt OR author:matt)
    
    This is useful where one field was indexed with a differently-analyzed copy
    of another, and you want the query to search both fields.
    
    You can specify a different group type with the ``group`` keyword. You can
    also specify ``group=None``, in which case the copied node is inserted
    "inline" next to the original, instead of in a new group::
    
        hello name:matt author:matt
    """
    
    def __init__(self, map, group=syntax.OrGroup, mirror=False):
        """
        :param map: a dictionary mapping names of fields to copy to the
            names of the destination fields.
        :param group: the type of group to create in place of the original
            token. You can specify ``group=None`` to put the copied node
            "inline" next to the original node instead of in a new group.
        :param two_way: if True, the plugin copies both ways, so if the user
            specifies a query in the 'toname' field, it will be copied to
            the 'fromname' field.
        """
        
        self.map = map
        self.group = group
        if mirror:
            # Add in reversed mappings
            map.update(dict((v, k) for k, v in iteritems(map)))
    
    def filters(self, parser):
        # Run after the fieldname filter (100) but before multifield (110)
        return [(self.do_copyfield, 109)]
    
    def do_copyfield(self, parser, group):
        map = self.map
        newgroup = group.empty_copy()
        for node in group:
            if isinstance(node, syntax.GroupNode):
                # Recurse into groups
                node = self.do_copyfield(parser, node)
            elif node.has_fieldname:
                fname = node.fieldname or parser.fieldname
                if fname in map:
                    newnode = copy.copy(node)
                    newnode.set_fieldname(map[fname], override=True)
                    if self.group is None:
                        newgroup.append(node)
                        newgroup.append(newnode)
                    else:
                        newgroup.append(self.group([node, newnode]))
                    continue
            newgroup.append(node)
        return newgroup









