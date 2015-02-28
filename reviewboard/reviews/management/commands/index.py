from datetime import datetime
import os
import optparse
import sys
import time

from django.conf import settings
from django.core.management.base import NoArgsCommand
from django.db.models import Q
from django.utils import timezone

from djblets.siteconfig.models import SiteConfiguration

from reviewboard.reviews.models import ReviewRequest
from reviewboard.reviews.models import Comment
try:
    import lucene
    lucene.initVM(lucene.CLASSPATH)
    have_lucene = True

    lv = [int(x) for x in lucene.VERSION.split('.')]
    lucene_is_2x = lv[0] == 2 and lv[1] < 9
    lucene_is_3x = lv[0] == 3 or (lv[0] == 2 and lv[1] == 9)
except ImportError:
    # This is here just in case someone is misconfigured but manages to
    # skip the dependency checks inside manage.py (perhaps they have
    # DEBUG = False)
    have_lucene = False

def flatten_comment(comment):
    text = comment.text
    for reply in comment._replies:
        text = text + "\n" + flatten_comment(reply)
    return text 

def get_all_review_comments(review_request):
    """
    Ripped from review_detail in views.py to get the inside track on extracting all comments from a review.
    """

    # The review request detail page needs a lot of data from the database,
    # and going through standard model relations will result in far too many
    # queries. So we'll be optimizing quite a bit by prefetching and
    # re-associating data.
    #
    # We will start by getting the list of reviews. We'll filter this out into
    # some other lists, build some ID maps, and later do further processing.
    entries = []
    public_reviews = []
    body_top_replies = {}
    body_bottom_replies = {}
    replies = {}
    reply_timestamps = {}
    reviews_entry_map = {}
    reviews_id_map = {}
    review_timestamp = 0

    # Start by going through all reviews that point to this review request.
    # We'll be separating these into a list of public reviews and a mapping of replies.
    #
    all_reviews = list(review_request.reviews.select_related('user'))

    for review in all_reviews:
        review._body_top_replies = []
        review._body_bottom_replies = []

        if review.public:
            # This is a review we'll display on the page. Keep track of it
            # for later display and filtering.
            public_reviews.append(review)
            parent_id = review.base_reply_to_id

            if parent_id is not None:
                # This is a reply to a review. We'll store the reply data
                # into a map, which associates a review ID with its list of
                # replies, and also figures out the timestamps.
                #
                # Later, we'll use this to associate reviews and replies for
                # rendering.
                if parent_id not in replies:
                    replies[parent_id] = [review]
                    reply_timestamps[parent_id] = review.timestamp
                else:
                    replies[parent_id].append(review)
                    reply_timestamps[parent_id] = max(
                        reply_timestamps[parent_id],
                        review.timestamp)

        if review.public:
            reviews_id_map[review.pk] = review

            # If this review is replying to another review's body_top or
            # body_bottom fields, store that data.
            for reply_id, reply_list in (
                (review.body_top_reply_to_id, body_top_replies),
                (review.body_bottom_reply_to_id, body_bottom_replies)):
                if reply_id is not None:
                    if reply_id not in reply_list:
                        reply_list[reply_id] = [review]
                    else:
                        reply_list[reply_id].append(review)

    review_ids = reviews_id_map.keys()
    last_visited = 0
    starred = False

    # Now that we have the list of public reviews and all that metadata,
    # being processing them and adding entries for display in the page.
    for review in public_reviews:
        if not review.is_reply():
            entry = {
                'review': review,
                'comments': {
                    'diff_comments': [],
                },
                'timestamp': review.timestamp,
            }
            reviews_entry_map[review.pk] = entry
            entries.append(entry)

    # Link up all the review body replies.
    for key, reply_list in (('_body_top_replies', body_top_replies),
                            ('_body_bottom_replies', body_bottom_replies)):
        for reply_id, replies in reply_list.iteritems():
            setattr(reviews_id_map[reply_id], key, replies)

    # Get all the comments and attach them to the reviews.
    for model, key, ordering in (
        (Comment, 'diff_comments',
         ('comment__filediff', 'comment__first_line', 'comment__timestamp')),):
        # Due to how we initially made the schema, we have a ManyToManyField
        # inbetween comments and reviews, instead of comments having a
        # ForeignKey to the review. This makes it difficult to easily go
        # from a comment to a review ID.
        #
        # The solution to this is to not query the comment objects, but rather
        # the through table. This will let us grab the review and comment in
        # one go, using select_related.
        related_field = model.review.related.field
        comment_field_name = related_field.m2m_reverse_field_name()
        through = related_field.rel.through
        q = through.objects.filter(review__in=review_ids).select_related()

        if ordering:
            q = q.order_by(*ordering)

        objs = list(q)

        # Two passes. One to build a mapping, and one to actually process
        # comments.
        comment_map = {}

        for obj in objs:
            comment = getattr(obj, comment_field_name)
            comment_map[comment.pk] = comment
            comment._replies = []

        for obj in objs:
            comment = getattr(obj, comment_field_name)

            # Short-circuit some object fetches for the comment by setting
            # some internal state on them.
            assert obj.review_id in reviews_id_map
            parent_review = reviews_id_map[obj.review_id]
            comment._review = parent_review
            comment._review_request = review_request

            if parent_review.is_reply():
                # This is a reply to a comment. Add it to the list of replies.
                assert obj.review_id not in reviews_entry_map
                assert parent_review.base_reply_to_id in reviews_entry_map

                # If there's an entry that isn't a reply, then it's
                # orphaned. Ignore it.
                if comment.is_reply():
                    replied_comment = comment_map[comment.reply_to_id]
                    replied_comment._replies.append(comment)
            elif parent_review.public:
                # This is a comment on a public review we're going to show.
                # Add it to the list.
                assert obj.review_id in reviews_entry_map
                entry = reviews_entry_map[obj.review_id]
                entry['comments'][key].append(comment)

    return entries

class Command(NoArgsCommand):
    option_list = NoArgsCommand.option_list + (
        optparse.make_option('--full', action='store_false',
                             dest='incremental', default=True,
                             help='Do a full (level-0) index of the database'),
        )
    help = "Creates a search index of review requests"
    requires_model_validation = True

    def handle_noargs(self, **options):
        siteconfig = SiteConfiguration.objects.get_current()

        # Refuse to do anything if they haven't turned on search.
        if not siteconfig.get("search_enable"):
            sys.stderr.write('Search is currently disabled. It must be '
                             'enabled in the Review Board administration '
                             'settings to run this command.\n')
            sys.exit(1)

        if not have_lucene:
            sys.stderr.write('PyLucene is required to build the search index.\n')
            sys.exit(1)

        incremental = options.get('incremental', True)

        store_dir = siteconfig.get("search_index_file")
        if not os.path.exists(store_dir):
            os.mkdir(store_dir)
        timestamp_file = os.path.join(store_dir, 'timestamp')

        timestamp = 0
        if incremental:
            try:
                f = open(timestamp_file, 'r')
                if timezone and settings.USE_TZ:
                    timestamp = timezone.make_aware(datetime.utcfromtimestamp(int(f.read())),
                                                    timezone.get_default_timezone())
                else:
                    timestamp = datetime.utcfromtimestamp(int(f.read()))
                f.close()
            except IOError:
                incremental = False

        f = open(timestamp_file, 'w')
        f.write('%d' % time.time())
        f.close()

        if lucene_is_2x:
            store = lucene.FSDirectory.getDirectory(store_dir, False)
            writer = lucene.IndexWriter(store, False,
                                        lucene.StandardAnalyzer(),
                                        not incremental)
        elif lucene_is_3x:
            store = lucene.FSDirectory.open(lucene.File(store_dir))
            writer = lucene.IndexWriter(store,
                lucene.StandardAnalyzer(lucene.Version.LUCENE_CURRENT),
                not incremental,
                lucene.IndexWriter.MaxFieldLength.LIMITED)
        else:
            assert False

        status = Q(status='P') | Q(status='S')
        objects = ReviewRequest.objects.filter(status)
        if incremental:
            query = Q(last_updated__gt=timestamp)
            # FIXME: re-index based on reviews once reviews are indexed.  I
            # tried ORing this in, but it doesn't seem to work.
            #        Q(review__timestamp__gt=timestamp)
            objects = objects.filter(query)

        if sys.stdout.isatty():
            print 'Creating Review Request Index'
        totalobjs = objects.count()
        i = 0
        prev_pct = -1

        for request in objects:
            try:
                # Remove the old documents from the index
                if incremental:
                    writer.deleteDocuments(lucene.Term('id', str(request.id)))

                self.index_review_request(writer, request)

                if sys.stdout.isatty():
                    i += 1
                    pct = (i * 100 / totalobjs)
                    if pct != prev_pct:
                        sys.stdout.write("  [%s%%]\r" % pct)
                        sys.stdout.flush()
                        prev_pct = pct

            except Exception, e:
                sys.stderr.write('Error indexing ReviewRequest #%d: %s\n' % \
                                 (request.id, e))

        if sys.stdout.isatty():
            print 'Optimizing Index'
        writer.optimize()

        if sys.stdout.isatty():
            print 'Indexed %d documents' % totalobjs
            print 'Done'

        writer.close()

    def index_review_request(self, writer, request):
        if lucene_is_2x:
            lucene_tokenized = lucene.Field.Index.TOKENIZED
            lucene_un_tokenized = lucene.Field.Index.UN_TOKENIZED
        elif lucene_is_3x:
            lucene_tokenized = lucene.Field.Index.ANALYZED
            lucene_un_tokenized = lucene.Field.Index.NOT_ANALYZED
        else:
            assert False

        # There are several fields we want to make available to users.
        # We index them individually, but also create a big hunk of text
        # to use for the default field, so people can just type in a
        # string and get results.
        doc = lucene.Document()
        doc.add(lucene.Field('id', str(request.id),
                             lucene.Field.Store.YES,
                             lucene.Field.Index.NO))
        doc.add(lucene.Field('summary', request.summary,
                             lucene.Field.Store.NO,
                             lucene_tokenized))
        if request.changenum:
            doc.add(lucene.Field('changenum',
                                 unicode(request.changenum),
                                 lucene.Field.Store.NO,
                                 lucene_tokenized))
        # Remove commas, since lucene won't tokenize it right with them
        bugs = ' '.join(request.bugs_closed.split(','))
        doc.add(lucene.Field('bug', bugs,
                             lucene.Field.Store.NO,
                             lucene_tokenized))

        name = ' '.join([request.submitter.username,
                         request.submitter.get_full_name()])
        doc.add(lucene.Field('author', name,
                             lucene.Field.Store.NO,
                             lucene_tokenized))
        doc.add(lucene.Field('username', request.submitter.username,
                             lucene.Field.Store.NO,
                             lucene_un_tokenized))
				
        comment_entries = get_all_review_comments(request)
        comment_text = ""
        for entry in comment_entries:
            review = entry["review"]
            comment_text = "\n".join([comment_text, review.body_top, review.body_bottom])
            review_top_replies = "\n".join(map(lambda x: "\n".join([x.body_top, x.body_bottom]), review._body_top_replies))
            review_bot_replies = "\n".join(map(lambda x: "\n".join([x.body_top, x.body_bottom]), review._body_bottom_replies))
            comment_text = "\n".join([comment_text, review_top_replies, review_bot_replies])
            comment_text += "\n".join(map(lambda x: flatten_comment(x), entry["comments"]["diff_comments"]))
        doc.add(lucene.Field('comment', comment_text,
                             lucene.Field.Store.NO,
                             lucene_tokenized))

        # FIXME: index dates

        files = []
        if request.diffset_history:
            for diffset in request.diffset_history.diffsets.all():
                for filediff in diffset.files.all():
                    if filediff.source_file:
                        files.append(filediff.source_file)
                    if filediff.dest_file:
                        files.append(filediff.dest_file)
        aggregate_files = '\n'.join(set(files))
        # FIXME: this tokenization doesn't let people search for files
        # in a really natural way.  It'll split on '/' which handles the
        # majority case, but it'd be nice to be able to drill down
        # (main.cc, vmuiLinux/main.cc, and player/linux/main.cc)
        doc.add(lucene.Field('file', aggregate_files,
                             lucene.Field.Store.NO,
                             lucene_tokenized))

        text = '\n'.join([request.summary,
                          request.description,
                          unicode(request.changenum),
                          request.testing_done,
                          bugs,
                          name,
                          comment_text,
                          aggregate_files])
        doc.add(lucene.Field('text', text,
                             lucene.Field.Store.NO,
                             lucene_tokenized))
        writer.addDocument(doc)
